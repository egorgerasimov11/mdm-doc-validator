#!/usr/bin/env python3
"""
patterns.py — the operator's PATTERN memory (D11d): every label / valid-mark
appends a PII-free shape fingerprint of the document, and future runs of the
SAME shape get an informational boost.

A pattern row holds NO values — only structure: doc_class, gold doc_type,
scenario tags, WHICH findings the operator effectively overrode (rule ids
whose effect was stricter than the operator's verdict), and which fields were
filled. Zero model calls on any path.

Consumption is deliberately modest (verdicts stay with the rules — invariant):
  * pipeline: >=3 matching VALID-marks -> a PATTERN-1 NOTE finding and the
    damping of ONE weak confidence signal (medium -> high); HARD signals and
    the verdict itself are never touched;
  * few-shot ranking and rule_stats read the same file as an extra signal.
"""
from __future__ import annotations

import json
import threading

from . import config
from .verdict import RANK

PATH_NAME = "patterns.jsonl"
_LOCK = threading.Lock()


def _path():
    return config.DATASET_DIR / PATH_NAME


def overridden_rule_ids(findings, operator_verdict: str) -> list[str]:
    """Findings whose verdict_effect is STRICTER than what the operator says
    the document deserves — the ones a valid-mark effectively overrides."""
    op = RANK.get(operator_verdict or "", 0)
    out = []
    for f in findings:
        d = f.to_dict() if hasattr(f, "to_dict") else dict(f)
        eff = d.get("verdict_effect")
        if eff and RANK.get(eff, 0) > op:
            out.append(str(d.get("rule_id")))
    return sorted(set(out))


def _field_shape(fields: dict) -> list[str]:
    out = []
    for k, v in (fields or {}).items():
        filled = (v.get("present") if isinstance(v, dict)
                  else bool(v) if isinstance(v, bool) else bool(str(v or "").strip()))
        if filled:
            out.append(k)
    return sorted(out)


def fingerprint(doc_class: str, doc_type: str, fields: dict,
                findings, operator_verdict: str) -> dict:
    return {"doc_class": doc_class, "doc_type": doc_type,
            "overridden": overridden_rule_ids(findings, operator_verdict),
            "field_shape": _field_shape(fields)}


def record(label: dict, findings, machine_verdict: str) -> dict:
    """Append one pattern row for a submitted label. PII-free by construction
    (structure only)."""
    fp = fingerprint(label.get("doc_class", ""), label.get("doc_type_gold", ""),
                     label.get("fields_gold", {}), findings,
                     label.get("verdict_gold", ""))
    row = {"ts": label.get("ts", ""),
           "doc_sha256": label.get("doc_sha256", ""),
           "valid": (label.get("verdict_gold") == "ACCEPT"
                     and bool(label.get("verdict_confirmed"))),
           "verdict_gold": label.get("verdict_gold", ""),
           "machine_verdict": machine_verdict,
           "scenarios": label.get("scenarios") or [],
           **fp}
    with _LOCK:
        config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def match_count(ext, findings) -> int:
    """How many DISTINCT documents of exactly this shape the operator marked
    VALID: same doc_class + doc_type, same overridden-finding set (vs ACCEPT),
    same filled-field shape."""
    fp = fingerprint(ext.doc_class, ext.doc_type,
                     ext.to_public(policy="masked").get("fields", {}),
                     findings, "ACCEPT")
    seen: set[str] = set()
    for row in load():
        if not row.get("valid"):
            continue
        if (row.get("doc_class") == fp["doc_class"]
                and row.get("doc_type") == fp["doc_type"]
                and row.get("overridden") == fp["overridden"]
                and row.get("field_shape") == fp["field_shape"]):
            seen.add(row.get("doc_sha256") or row.get("ts", ""))
    return len(seen)
