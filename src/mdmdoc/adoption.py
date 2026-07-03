#!/usr/bin/env python3
"""
adoption.py — the model adoption gate.

A retrain never silently becomes the production extractor anymore. Flow:

    build candidate (mdmdoc-extract-candidate, from current prompts+few-shot)
      -> gated eval (full pipeline with TEXT=candidate; history NOT touched)
      -> gate: leakage == 0, invoice false-accepts == 0, no critical-field
         regressions vs the adopted baseline
      -> operator clicks Adopt (promote) or Rollback (rebuild from the
         previous Modelfile, which is always kept as the rollback target).

State lives in models/adoption.json; Modelfile.mdmdoc-extract.previous is the
rollback target. Promotion uses `ollama cp` so the evaluated weights/prompt are
EXACTLY what production gets.
"""
from __future__ import annotations

import json
import shutil

from . import config, model_client as mc, runstore
from .modelfile import CANDIDATE_NAME, MODEL_NAME, build_modelfile, ollama_cmd

# a candidate that drops accuracy on any of these fields is not adoptable —
# they are the fields SAP replication actually keys on
CRITICAL_FIELDS = ("bank.iban", "bank.account_number", "bank.swift_bic",
                   "w9.tin", "w9.line3_classification")

STATE_PATH = config.MODELS_DIR / "adoption.json"
ADOPTED_MF = config.MODELS_DIR / f"Modelfile.{MODEL_NAME}"
PREVIOUS_MF = config.MODELS_DIR / f"Modelfile.{MODEL_NAME}.previous"
CANDIDATE_MF = config.MODELS_DIR / f"Modelfile.{CANDIDATE_NAME}"
CANDIDATE_RESULTS = "candidate_results.json"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    """State holds metrics/timestamps/gate reasons only — still leak-gated
    (strict) like every other write choke point."""
    from .privacy import assert_no_leak
    config.ensure_dirs()
    blob = json.dumps(state, ensure_ascii=False, indent=1)
    assert_no_leak(blob)
    STATE_PATH.write_text(blob)


def gate_check(candidate: dict, baseline: dict | None) -> tuple[bool, list[str]]:
    """Pure decision function: may this candidate become mdmdoc-extract?
    candidate/baseline are eval metrics dicts (baseline None = first ever eval:
    only the absolute conditions apply)."""
    reasons: list[str] = []
    if candidate.get("leakage_count"):
        reasons.append(f"leakage_count = {candidate['leakage_count']} (must be 0)")
    if candidate.get("invoice_false_accept_rate"):
        reasons.append(f"invoice_false_accept_rate = "
                       f"{candidate['invoice_false_accept_rate']} (must be 0)")
    if baseline:
        cf, bf = candidate.get("fields") or {}, baseline.get("fields") or {}
        for k in CRITICAL_FIELDS:
            if k in cf and k in bf and cf[k] < bf[k]:
                reasons.append(f"critical field {k} regressed: {bf[k]} -> {cf[k]}")
    return (not reasons), reasons


def _progress_print(progress):
    def _p(line: str) -> None:
        print(line)
        if progress:
            progress(line)
    return _p


def build_candidate(progress=None) -> dict:
    """Build mdmdoc-extract-candidate from the current prompts + few-shot.
    Production (mdmdoc-extract) is NOT touched — this is where every retrain
    lands now; promotion only happens through the gate."""
    _p = _progress_print(progress)
    _p(f"building candidate model {CANDIDATE_NAME}…")
    rc = build_modelfile(apply=True, name=CANDIDATE_NAME)
    if rc != 0:
        raise RuntimeError(f"candidate build failed (rc={rc})")
    mc.reset_host()   # the new model must be visible to resolve()
    state = load_state()
    state["candidate"] = {"model": CANDIDATE_NAME, "built_ts": runstore.now_iso(),
                          "evaluated": False, "gate_ok": False,
                          "gate_reasons": ["not evaluated yet"]}
    _save_state(state)
    return state


def evaluate_candidate(progress=None) -> dict:
    """Gated eval of the existing candidate: full pipeline with TEXT=candidate;
    eval history is NOT touched. Caller (server job / CLI) holds PIPELINE_LOCK
    semantics the same way plain eval does."""
    _p = _progress_print(progress)
    from .evalrun import run_eval

    state = load_state()
    if not state.get("candidate"):
        raise RuntimeError("no candidate built — build one first")
    baseline = latest_baseline()
    _p("evaluating candidate over all labels (history is NOT touched)…")
    old_text = mc.ROLES["TEXT"]
    mc.ROLES["TEXT"] = CANDIDATE_NAME
    try:
        run_eval(tag=f"candidate:{CANDIDATE_NAME}", progress=progress,
                 record=False, results_name=CANDIDATE_RESULTS)
    finally:
        mc.ROLES["TEXT"] = old_text

    results_path = config.EVAL_DIR / CANDIDATE_RESULTS
    results = json.loads(results_path.read_text()) if results_path.exists() else {}
    metrics = results.get("metrics") or {}
    ok, reasons = gate_check(metrics, baseline)

    state["candidate"].update({
        "ts": runstore.now_iso(), "evaluated": True,
        "metrics": metrics, "diff": results.get("diff") or {},
        "baseline": baseline, "gate_ok": ok, "gate_reasons": reasons,
    })
    _save_state(state)
    _p(("GATE PASSED — candidate may be adopted" if ok
        else "GATE FAILED: " + "; ".join(reasons)))
    return state


def build_and_eval_candidate(progress=None) -> dict:
    build_candidate(progress)
    return evaluate_candidate(progress)


def latest_baseline() -> dict | None:
    """Metrics of the adopted model's most recent recorded eval."""
    p = config.EVAL_DIR / "history.jsonl"
    if not p.exists():
        return None
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1]).get("metrics")
    except Exception:
        return None


def adopt() -> dict:
    """Promote the evaluated candidate to mdmdoc-extract. The previously adopted
    Modelfile is kept as the rollback target."""
    state = load_state()
    cand = state.get("candidate")
    if not cand:
        raise RuntimeError("no evaluated candidate — build & evaluate one first")
    if not cand.get("gate_ok"):
        raise RuntimeError("gate failed — fix the regressions or rollback: "
                           + "; ".join(cand.get("gate_reasons") or []))
    if not CANDIDATE_MF.exists():
        raise RuntimeError(f"candidate Modelfile missing ({CANDIDATE_MF})")
    rc = ollama_cmd(["ollama", "cp", CANDIDATE_NAME, MODEL_NAME])
    if rc != 0:
        raise RuntimeError(f"ollama cp failed (rc={rc})")
    if ADOPTED_MF.exists():
        shutil.copy2(ADOPTED_MF, PREVIOUS_MF)
    shutil.copy2(CANDIDATE_MF, ADOPTED_MF)
    mc.reset_host()
    state["previous"] = state.get("adopted")
    state["adopted"] = {"ts": runstore.now_iso(), "metrics": cand.get("metrics"),
                        "from_candidate_ts": cand.get("ts")}
    state.pop("candidate", None)
    _save_state(state)
    return state


def rollback() -> dict:
    """Rebuild mdmdoc-extract from the previous Modelfile (the rollback target)."""
    if not PREVIOUS_MF.exists():
        raise RuntimeError(f"no rollback target ({PREVIOUS_MF} missing) — "
                           "nothing was adopted over yet")
    rc = ollama_cmd(["ollama", "create", MODEL_NAME, "-f", str(PREVIOUS_MF)])
    if rc != 0:
        raise RuntimeError(f"ollama create from previous Modelfile failed (rc={rc})")
    shutil.copy2(PREVIOUS_MF, ADOPTED_MF)
    mc.reset_host()
    state = load_state()
    state["adopted"] = {"ts": runstore.now_iso(), "rolled_back": True,
                        "metrics": (state.get("previous") or {}).get("metrics")}
    state.pop("candidate", None)
    _save_state(state)
    return state
