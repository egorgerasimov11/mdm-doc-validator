#!/usr/bin/env python3
"""
run_golden.py — run the golden I/O parity corpus through the Python DETERMINISTIC
engine (no LLM) and check each case's expected doc_type / fields / crosscheck
notes. The SAME cases feed a generated ABAP data class (gen_abap_golden.py) so
the two implementations must reproduce identical field/note outputs — a
behavioural parity check that a marker grep cannot give.

`tools/golden/golden_cases.json` is the single source of truth:
  {version, cases: [{id, doc_class, filename, raw_text,
                     expect: {doc_type?, fields?: {k: v}, notes?: [substr, ...]}}]}

Field expectations compare the PUBLIC masked view (extraction.to_public) so no
real value is ever asserted; notes are substring checks on the crosscheck list.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mdmdoc import ocr  # noqa: E402
from mdmdoc.fields import find_boxed_tin, type_hint  # noqa: E402
from mdmdoc.stage_a import RawDoc  # noqa: E402
from mdmdoc.stage_b import extract  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "golden_cases.json"


def _build_raw(case: dict) -> RawDoc:
    """Minimal RawDoc the deterministic engine needs: text + candidates +
    type_hint (mirrors the relevant tail of stage_a.perceive)."""
    dc = case["doc_class"]
    fn = case.get("filename", f"{case['id']}.pdf")
    text = case["raw_text"]
    raw = RawDoc(path=f"/golden/{fn}", sha256=hashlib.sha256(text.encode()).hexdigest(),
                 ext=".pdf", doc_class=dc)
    raw.raw_text = text
    raw.has_text_layer = True
    raw.regex_candidates = ocr.regex_fields(text)
    if dc == "w9" and "ein" not in raw.regex_candidates:
        boxed, boxed_type = find_boxed_tin(text)
        if boxed:
            raw.regex_candidates["tin_boxed"] = boxed
            if boxed_type:
                raw.regex_candidates["tin_boxed_type"] = boxed_type
    raw.type_hint = type_hint(fn, text, ".pdf", dc)
    return raw


def run_case(case: dict) -> dict:
    """-> {id, ok, fails: [str]} — deterministic, no model."""
    raw = _build_raw(case)
    ext = extract(raw, engine="deterministic")
    pub = ext.to_public(policy="masked")
    exp = case.get("expect", {})
    fails: list[str] = []
    if "doc_type" in exp and pub.get("doc_type") != exp["doc_type"]:
        fails.append(f"doc_type={pub.get('doc_type')} != {exp['doc_type']}")
    for k, want in (exp.get("fields") or {}).items():
        got = pub.get("fields", {}).get(k)
        got = (got.get("masked") if isinstance(got, dict) else got)
        if str(got or "") != str(want):
            fails.append(f"field {k}={got!r} != {want!r}")
    notes = " ".join(str(n) for n in (pub.get("crosscheck") or []))
    for sub in exp.get("notes") or []:
        if sub not in notes:
            fails.append(f"missing note substring: {sub!r}")
    return {"id": case["id"], "ok": not fails, "fails": fails}


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text())["cases"]


def corpus_hash() -> str:
    """Stable hash of the case corpus — check_parity §7 compares it to the ABAP
    generated-data header so a corpus edit that skips regeneration is caught."""
    return hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()[:16]


def main() -> int:
    results = [run_case(c) for c in load_cases()]
    bad = [r for r in results if not r["ok"]]
    for r in results:
        print(("ok  " if r["ok"] else "FAIL") + f" {r['id']}"
              + ("" if r["ok"] else "  — " + "; ".join(r["fails"])))
    print(f"\n{len(results) - len(bad)}/{len(results)} golden cases pass "
          f"(corpus {corpus_hash()})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
