"""F6: the golden deterministic-parity corpus all passes on the Python side.
The SAME cases are generated into an ABAP data class (tools/golden/gen_abap_
golden.py) so the twin must reproduce identical fields/notes — check_parity §7
verifies the ABAP copy is up to date with the corpus hash."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "golden"))

import run_golden  # noqa: E402


def test_golden_corpus_all_pass():
    results = [run_golden.run_case(c) for c in run_golden.load_cases()]
    bad = [r for r in results if not r["ok"]]
    assert not bad, "golden cases failed: " + "; ".join(
        f"{r['id']}: {r['fails']}" for r in bad)


def test_corpus_hash_stable():
    # deterministic 16-hex — feeds the ABAP generated-data freshness check
    h = run_golden.corpus_hash()
    assert len(h) == 16 and h == run_golden.corpus_hash()


def test_golden_asserts_verdicts_deeply():
    """M3: the corpus must pin VERDICT parity, not just fields/notes — at least
    5 cases carry an expected verdict, covering every non-trivial level."""
    cases = run_golden.load_cases()
    verdicts = [c["expect"]["verdict"] for c in cases if "verdict" in c.get("expect", {})]
    assert len(verdicts) >= 5
    assert {"REJECT", "NEED_MANUAL_REVIEW", "WARNING", "ACCEPT"} <= set(verdicts)
    with_findings = [c for c in cases if c.get("expect", {}).get("findings")]
    assert len(with_findings) >= 5


def test_golden_no_full_tin_or_iban_in_expectations():
    """Masking parity: expectations must never carry a full TIN/IBAN — the
    masked forms are themselves the cross-implementation assertion."""
    import json as _json
    blob = _json.dumps(run_golden.load_cases(), ensure_ascii=False)
    # the w9 EIN appears in raw_text (input), but expectations only masked:
    expect_blob = _json.dumps([c.get("expect", {}) for c in run_golden.load_cases()],
                              ensure_ascii=False)
    assert "45-3859289" not in expect_blob and "453859289" not in expect_blob
    assert "XX-XXX9289" in expect_blob      # the masked form IS asserted
    assert blob  # corpus loads


def test_injected_llm_fields_run_guard_chain():
    """The injected_llm seam must run the REAL guard/crosscheck chain, not
    bypass it (mirrors ABAP build(it_llm_fields))."""
    from mdmdoc.stage_b import extract
    case = next(c for c in run_golden.load_cases()
                if c["id"] == "iban-model-vs-ocr-mismatch")
    raw = run_golden._build_raw(case)
    ext = extract(raw, engine="deterministic", injected_llm=case["llm_fields"])
    assert any(n.startswith("iban=MISMATCH") for n in ext.crosscheck)
    # deterministic warning about skipped LLM must NOT appear (fields were seeded)
    assert not any("LLM extraction skipped" in w for w in ext.warnings)
