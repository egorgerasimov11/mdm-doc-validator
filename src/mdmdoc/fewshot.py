#!/usr/bin/env python3
"""
fewshot.py — turn labeled corrections into Stage-B prompt exemplars.

Exemplar sensitive values are shape-preserving FAKES (from the label's
sensitive_map), never real and never masks: masked exemplars would teach the
model to emit masks, fakes teach it to copy exact-looking values. Selection
is greedy scenario-COVERAGE (each pick must show the model a situation the
already-picked exemplars don't), tie-broken by teaching value (cases the
model got wrong teach the most). Labels without scenario tags still cover
their doc type, so the old doc-type diversity is the degenerate case.
"""
from __future__ import annotations

import json
import re

from . import config
from .dataset import load_labels
from .fields import BANK_KEYS, W9_KEYS
from .privacy import assert_no_leak


def _defake(text: str, sensitive_map: list[dict]) -> str:
    """Replace masked forms in an excerpt with the corresponding fake full values."""
    t = text or ""
    for it in sensitive_map or []:
        if it.get("masked") and it.get("fake"):
            t = t.replace(it["masked"], it["fake"])
    return t


def _fake_for(masked: str, sensitive_map: list[dict]) -> str:
    for it in sensitive_map or []:
        if it.get("masked") == masked:
            return it.get("fake", "")
    return ""


def label_to_example(lab: dict) -> dict:
    smap = lab.get("sensitive_map", [])
    gold = lab.get("fields_gold", {})
    keys = BANK_KEYS if lab["doc_class"] == "bank" else W9_KEYS
    out_fields: dict = {}
    for k in keys:
        if k == "tin_type":
            tt = (gold.get("tin") or {}).get("type", "")
            out_fields[k] = tt if tt in ("SSN", "EIN") else ""
            continue
        if k == "tin_raw":
            tin = gold.get("tin") or {}
            out_fields[k] = _fake_for(tin.get("masked", ""), smap) if tin.get("present") else ""
            continue
        v = gold.get(k, "")
        if isinstance(v, dict):
            out_fields[k] = _fake_for(v.get("masked", ""), smap) if v.get("present") else ""
        else:
            out_fields[k] = v
    # drop candidates whose mask has no registered fake — a residual mask in an
    # exemplar would teach the model to OUTPUT masks instead of exact values
    cand = {}
    for k, v in (lab.get("regex_candidates_masked") or {}).items():
        dv = _defake(str(v), smap)
        if not re.search(r"[*…]|XXX?-", dv):
            cand[k] = dv
    # keep exemplars short: long exemplars crowd the real document out of context
    excerpt = _defake(lab.get("stage_a_excerpt", ""), smap)[-800:]
    inp = ("OCR-VERIFIED CANDIDATES:\n" + json.dumps(cand, ensure_ascii=False)
           + "\n\nDOCUMENT TEXT:\n" + excerpt)
    return {"input": inp, "output": {"doc_type": lab.get("doc_type_gold", ""), "fields": out_fields}}


def _completeness(lab: dict) -> int:
    """Exemplars with empty core fields teach the model to LEAVE fields empty —
    prefer complete golds."""
    core = (("account_holder", "bank_name") if lab.get("doc_class") == "bank"
            else ("line1_name", "line3_classification", "tin"))
    gold = lab.get("fields_gold", {})
    n = 0
    for k in core:
        v = gold.get(k, "")
        n += int(bool(v.get("present")) if isinstance(v, dict) else bool(str(v or "").strip()))
    return n


def _teaching_value(lab: dict) -> int:
    mp = lab.get("model_predicted", {}) or {}
    score = len(mp.get("fields_diff") or {})
    if mp.get("doc_type") and mp.get("doc_type") != lab.get("doc_type_gold"):
        score += 3
    return score + 2 * _completeness(lab)


def _coverage_units(lab: dict) -> set:
    """What situations this exemplar demonstrates: its scenario tags plus the
    doc type (so untagged labels still diversify by type)."""
    units = set(lab.get("scenarios") or [])
    units.add(f"type:{lab.get('doc_type_gold', '?')}")
    return units


def _pick_by_coverage(pool: list[dict], k: int) -> list[dict]:
    """Greedy max-coverage: each pick adds the most not-yet-shown scenario
    units; ties go to the higher teaching value. Recency plays no part."""
    ordered = sorted(pool, key=_teaching_value, reverse=True)
    picked: list[dict] = []
    covered: set = set()
    while len(picked) < k and ordered:
        best = max(ordered, key=lambda l: (len(_coverage_units(l) - covered),
                                           _teaching_value(l)))
        ordered.remove(best)
        picked.append(best)
        covered |= _coverage_units(best)
    return picked


def build_fewshot(k: int = 2) -> int:
    config.ensure_dirs()
    labels = [l for l in load_labels() if l.get("confirmed")]
    if not labels:
        print("no confirmed labels — label some runs first (`mdmdoc review last`)")
        return 1
    for doc_class in ("bank", "w9"):
        pool = [l for l in labels if l.get("doc_class") == doc_class]
        picked = _pick_by_coverage(pool, k)
        examples = [label_to_example(l) for l in picked]
        blob = json.dumps(examples, ensure_ascii=False, indent=2)
        fakes = [f for l in picked for f in
                 (it.get("fake", "") for it in l.get("sensitive_map", []))]
        assert_no_leak(blob, allowed_fakes=[f for f in fakes if f])
        p = config.FEWSHOT_DIR / f"{doc_class}.json"
        p.write_text(blob, encoding="utf-8")
        print(f"{doc_class}: wrote {len(examples)} exemplars -> {p}")
    print("Few-shot prompts updated. Run `mdmdoc eval --tag after-fewshot` to measure the effect.")
    return 0
