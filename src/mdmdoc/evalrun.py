#!/usr/bin/env python3
"""
evalrun.py — regression metrics over dataset/labels.jsonl. Re-runs the FULL
pipeline per label (no cached raw text exists by design — full text is never
persisted), scores extraction + verdicts, sweeps the tree for leaks, and writes
eval/report.md with a delta vs the previous run.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, runstore
from .dataset import load_labels
from .pipeline import run_check
from .privacy import assert_no_leak

BANK_SCORED = ["account_holder", "bank_name", "iban", "swift_bic", "account_number"]
W9_SCORED = ["line1_name", "line2_business_name", "line3_classification", "tin"]


def _norm(s) -> str:
    from .fields import _norm_name
    return _norm_name(str(s or ""))


def _field_match(key: str, pred_fields: dict, gold_fields: dict) -> bool:
    pk = "tin" if key == "tin" else key
    pv, gv = pred_fields.get(pk, ""), gold_fields.get(pk, "")
    if isinstance(gv, dict) or isinstance(pv, dict):
        pv = pv if isinstance(pv, dict) else {"present": bool(str(pv or "").strip()), "masked": str(pv or "")}
        gv = gv if isinstance(gv, dict) else {"present": bool(str(gv or "").strip()), "masked": str(gv or "")}
        if not gv.get("present") and not pv.get("present"):
            return True
        if gv.get("present") != pv.get("present"):
            return False
        if key == "tin" and _norm(gv.get("type")) != _norm(pv.get("type")):
            return False
        return gv.get("masked") == pv.get("masked")
    return _norm(pv) == _norm(gv)


def _leak_sweep() -> int:
    """dataset/prompts/eval are ALWAYS strict; runs/ follows the operator's
    display policy (full banking values there are legitimate, TIN never is)."""
    from .dataset import all_fakes
    fakes = all_fakes()
    hits = 0
    roots = ((config.RUNS_DIR, config.gate_policy()),
             (config.DATASET_DIR, "strict"),
             (config.FEWSHOT_DIR, "strict"),
             (config.EVAL_DIR, "strict"))
    for root, pol in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in (".json", ".jsonl", ".md", ".txt"):
                continue
            try:
                hits += len(assert_no_leak(p.read_text(encoding="utf-8", errors="replace"),
                                           allowed_fakes=fakes, raise_on_hit=False,
                                           policy=pol))
            except Exception:
                continue
    return hits


def scenario_slices(rows: list[dict]) -> dict:
    """Per-scenario regression buckets from scored rows (pure, unit-testable).
    A row counts toward every scenario tag on its label."""
    slices: dict = {}
    for r in rows:
        if "error" in r:
            continue
        for tag in r.get("scenarios") or []:
            s = slices.setdefault(tag, {"n": 0, "ok": 0, "type_ok": 0, "verdict_ok": 0})
            s["n"] += 1
            s["ok"] += int(bool(r.get("ok")))
            s["type_ok"] += int(bool(r.get("type_ok")))
            s["verdict_ok"] += int(bool(r.get("verdict_ok")))
    return {tag: {"n": s["n"],
                  "accuracy": round(s["ok"] / s["n"], 3),
                  "doc_type_accuracy": round(s["type_ok"] / s["n"], 3),
                  "verdict_accuracy": round(s["verdict_ok"] / s["n"], 3)}
            for tag, s in sorted(slices.items())}


def run_eval(only: str | None = None, limit: int | None = None, tag: str = "",
             progress=None, scenario: str | None = None, record: bool = True,
             results_name: str = "last_results.json") -> int:
    """progress: optional Callable[[str], None] for live line-by-line status
    (used by the server's job log); CLI output is unchanged.
    scenario: only labels carrying that scenario tag.
    record=False (candidate/model-adoption evals): write results to results_name
    but do NOT touch history.jsonl or the main report — gate evals must never
    masquerade as the adopted model's track record."""
    def _p(line: str) -> None:
        if progress:
            progress(line)

    config.ensure_dirs()
    labels = [l for l in load_labels() if not only or l.get("doc_class") == only]
    if scenario:
        labels = [l for l in labels if scenario in (l.get("scenarios") or [])]
    if limit:
        labels = labels[:limit]
    if not labels:
        print("no labels yet — run some checks and label them with `mdmdoc review`"
              + (f" (no label carries scenario {scenario!r})" if scenario else ""))
        return 1

    rows = []
    type_hits = verdict_hits = json_ok = 0
    invoice_total = invoice_false_accept = 0
    field_stats: dict = {}
    confusion: dict = {}
    for lab in labels:
        path = Path(lab["doc_path"])
        if not path.exists():
            rows.append({"file": lab["doc_path"], "error": "missing file"})
            continue
        try:
            # precedents OFF: eval measures the machine, not stored operator answers
            res = run_check(path, lab["doc_class"], apply_precedent=False, web_evidence=False,
                            enforce_approvals=False)
        except Exception as e:  # noqa: BLE001 — eval must survive any doc
            from .privacy import scrub_text
            rows.append({"file": path.name, "error": scrub_text(str(e))})
            continue
        pred_type = res.pub.get("doc_type", "")
        gold_type = lab.get("doc_type_gold", "")
        type_hits += int(pred_type == gold_type)
        confusion.setdefault(gold_type, {}).setdefault(pred_type, 0)
        confusion[gold_type][pred_type] += 1
        verdict_hits += int(res.verdict == lab.get("verdict_gold"))
        json_ok += int(bool(res.pub.get("json_valid_first_try")))
        if gold_type == "invoice":
            invoice_total += 1
            invoice_false_accept += int(res.verdict != "REJECT")
        scored = BANK_SCORED if lab["doc_class"] == "bank" else W9_SCORED
        row_fields = {}
        for k in scored:
            ok = _field_match(k, res.pub.get("fields", {}), lab.get("fields_gold", {}))
            st = field_stats.setdefault(f"{lab['doc_class']}.{k}", [0, 0])
            st[0] += int(ok)
            st[1] += 1
            row_fields[k] = ok
        rows.append({"file": path.name, "run_id": res.run_id,
                     "doc_class": lab["doc_class"],
                     "doc_type": f"{pred_type}/{gold_type}",
                     "verdict": f"{res.verdict}/{lab.get('verdict_gold')}",
                     "tier": res.pub.get("tier", "fast"),
                     "type_ok": pred_type == gold_type,
                     "verdict_ok": res.verdict == lab.get("verdict_gold"),
                     "scenarios": lab.get("scenarios") or [],
                     "ok": (pred_type == gold_type and res.verdict == lab.get("verdict_gold")
                            and all(row_fields.values())),
                     "fields": row_fields})
        _p(f"[{len(rows)}/{len(labels)}] {path.name}: {res.verdict}/{lab.get('verdict_gold')}"
           + (f", misses: {', '.join(k for k, ok in row_fields.items() if not ok)}"
              if any(not ok for ok in row_fields.values()) else ""))

    n = sum(1 for r in rows if "error" not in r)
    leaks = _leak_sweep()
    metrics = {
        "n": n,
        "doc_type_accuracy": round(type_hits / n, 3) if n else 0,
        "verdict_accuracy": round(verdict_hits / n, 3) if n else 0,
        "json_valid_first_try": round(json_ok / n, 3) if n else 0,
        "invoice_false_accept_rate": (round(invoice_false_accept / invoice_total, 3)
                                      if invoice_total else None),
        "fields": {k: round(v[0] / v[1], 3) for k, v in sorted(field_stats.items())},
        "scenarios": scenario_slices(rows),
        "leakage_count": leaks,
    }

    # structured per-doc results + regression diff vs the previous MAIN eval
    # (candidate evals diff against the adopted model's last results, read-only)
    main_results_path = config.EVAL_DIR / "last_results.json"
    results_path = config.EVAL_DIR / results_name
    prev_rows = {}
    if main_results_path.exists():
        try:
            prev_rows = {r["file"]: r
                         for r in json.loads(main_results_path.read_text()).get("rows", [])
                         if isinstance(r, dict)}
        except Exception:
            prev_rows = {}
    diff = {"improved": [], "regressed": [], "unchanged_wrong": []}
    for r in rows:
        if "error" in r:
            continue
        prev = prev_rows.get(r["file"])
        if prev is None or "ok" not in prev:
            continue
        if r["ok"] and not prev["ok"]:
            diff["improved"].append(r["file"])
        elif not r["ok"] and prev["ok"]:
            diff["regressed"].append(r["file"])
        elif not r["ok"] and not prev["ok"]:
            diff["unchanged_wrong"].append(r["file"])
    results_path.write_text(json.dumps(
        {"ts": runstore.now_iso(), "tag": tag, "scenario": scenario, "rows": rows,
         "diff": diff, "metrics": metrics},
        ensure_ascii=False, indent=1))

    # history + delta
    hist_path = config.EVAL_DIR / "history.jsonl"
    prev = None
    if hist_path.exists():
        lines = [l for l in hist_path.read_text().splitlines() if l.strip()]
        if lines:
            prev = json.loads(lines[-1])
    entry = {"ts": runstore.now_iso(), "tag": tag, "metrics": metrics}
    if record:
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # report
    lines = [f"# mdmdoc eval — {entry['ts']}" + (f" ({tag})" if tag else ""), ""]
    lines.append(f"Labels evaluated: {n} (errors: {len(rows) - n})"
                 + (f", scenario filter: {scenario}" if scenario else ""))
    lines.append("")
    lines.append("| metric | value |" + (" previous |" if prev else ""))
    lines.append("|---|---|" + ("---|" if prev else ""))
    for k in ("doc_type_accuracy", "verdict_accuracy", "json_valid_first_try",
              "invoice_false_accept_rate", "leakage_count"):
        pv = f" {prev['metrics'].get(k)} |" if prev else ""
        lines.append(f"| {k} | {metrics.get(k)} |{pv}")
    lines.append("")
    lines.append("## Per-field exact match")
    for k, v in metrics["fields"].items():
        pv = ""
        if prev:
            pv = f" (prev {prev['metrics'].get('fields', {}).get(k, '—')})"
        lines.append(f"- {k}: {v}{pv}")
    if metrics["scenarios"]:
        lines.append("")
        lines.append("## Scenario slices")
        for s, m in metrics["scenarios"].items():
            lines.append(f"- {s}: {m['accuracy']} of {m['n']} fully correct "
                         f"(doc_type {m['doc_type_accuracy']}, verdict {m['verdict_accuracy']})")
    lines.append("")
    lines.append("## Confusion (gold -> predicted)")
    for g, preds in sorted(confusion.items()):
        lines.append(f"- {g or '??'}: " + ", ".join(f"{p or '??'}×{c}" for p, c in sorted(preds.items())))
    lines.append("")
    lines.append("## Per-document")
    for r in rows:
        if "error" in r:
            lines.append(f"- {r['file']}: ERROR {r['error']}")
        else:
            bad = [k for k, ok in r["fields"].items() if not ok]
            lines.append(f"- {r['file']}: type {r['doc_type']}, verdict {r['verdict']}"
                         + (f", field misses: {', '.join(bad)}" if bad else ", all fields OK"))
    report = "\n".join(lines) + "\n"
    report_name = "report.md" if record else results_name.replace("_results.json", "_report.md")
    (config.EVAL_DIR / report_name).write_text(report, encoding="utf-8")
    print(report)
    _p(f"done: doc_type={metrics['doc_type_accuracy']} verdict={metrics['verdict_accuracy']} "
       f"leakage={leaks}")
    if leaks:
        print(f"FAIL: sensitive-leakage count = {leaks} (must be 0)")
        _p(f"FAIL: sensitive-leakage count = {leaks} (must be 0)")
        return 1
    return 0
