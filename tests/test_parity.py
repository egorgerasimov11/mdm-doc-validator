"""The drift detector's pure parsers must stay correct, and the manifest must
list exactly the predicates Python implements (so a new predicate can't be added
without a conscious parity entry). A missing ABAP checkout FAILS the
cross-repo tests (M2 — no vacuous green) unless MDMDOC_PARITY_OPTIONAL=1."""
import os
import sys
from pathlib import Path

import pytest

PY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PY_ROOT))  # make the repo-root `tools/` package importable

from tools import check_parity as cp  # noqa: E402


def _need_abap() -> None:
    """Cross-repo precondition: fail loudly when the ABAP tree is absent (the
    old skipif silently passed a gate that verified nothing)."""
    if cp.ABAP_ROOT.exists():
        return
    if os.environ.get("MDMDOC_PARITY_OPTIONAL") == "1":
        pytest.skip("ABAP checkout absent — skipped by MDMDOC_PARITY_OPTIONAL=1")
    pytest.fail(f"ABAP repo not found at {cp.ABAP_ROOT} — parity NOT verified. "
                "Run `git submodule update --init abap` or clone the sibling, "
                "or export MDMDOC_PARITY_OPTIONAL=1 to skip consciously.")

# the canonical predicate set — if this changes, both ABAP and PARITY.md must follow
PREDICATES = {"unsigned_no_evidence", "unsigned_typed_block", "field_empty",
              "no_bank_ids", "swift_valid", "iban_valid", "ein_shape",
              "tin_type_vs_classification", "individual_with_business_name_and_ein",
              "line_swap_suspect", "date_older_than"}


def test_python_predicates_match_registry():
    assert cp.python_predicates(PY_ROOT) == PREDICATES


def test_manifest_lists_exactly_the_predicates():
    listed, _pending, _guards = cp.parity_manifest(PY_ROOT)
    assert listed == PREDICATES, "PARITY.md predicate list drifted from REGISTRY"


def test_guard_manifest_covers_all_stage_b_guards():
    # every deterministic stage_b guard needs a conscious PARITY.md status
    _listed, _pending, guards = cp.parity_manifest(PY_ROOT)
    assert cp.python_guards(PY_ROOT) == set(guards), \
        "stage_b guards vs PARITY.md '## Guards' drifted"
    assert set(guards.values()) <= {"ported", "n/a", "pending"}


def test_ported_guards_have_abap_markers():
    _need_abap()
    _listed, _pending, guards = cp.parity_manifest(PY_ROOT)
    markers = cp.abap_guard_markers()
    ported = {g for g, s in guards.items() if s == "ported"}
    assert ported <= markers, f"missing ABAP [GUARD:x] markers: {ported - markers}"
    assert markers <= set(guards), f"ABAP markers without manifest entry: {markers - set(guards)}"


def test_yaml_rules_and_checks_parse():
    rules = cp.yaml_rules(PY_ROOT / "rules" / "banking.yaml")
    assert "BNK-001" in rules and "__tables__" in rules
    used = cp.yaml_checks_used(PY_ROOT / "rules" / "banking.yaml")
    assert "iban_valid" in used and used <= PREDICATES


def test_abap_dispatch_matches_python_predicates():
    _need_abap()
    # the ABAP rule engine must dispatch exactly the predicates Python implements
    assert cp.abap_check_names() == PREDICATES


def test_rule_data_identical_between_repos():
    _need_abap()
    for cls in ("banking", "w9"):
        py = cp.yaml_rules(PY_ROOT / "rules" / f"{cls}.yaml")
        abap = cp.yaml_rules(cp.ABAP_ROOT / "rules" / f"{cls}.yaml")
        assert py == abap, f"{cls}.yaml differs between Python and ABAP repos"


def test_golden_regen_diff_flags_hand_edit(tmp_path, monkeypatch):
    """§7 M4: a hand-edit of the generated ABAP golden class must fail parity
    even when both hash headers are intact."""
    _need_abap()
    import shutil
    fake_abap = tmp_path / "abap"
    (fake_abap / "src").mkdir(parents=True)
    src = cp.ABAP_ROOT / "src" / "zcl_mdmdoc_golden_data.clas.abap"
    tampered = src.read_text().replace("`bank`", "`bank`", 1)  # start byte-equal
    (fake_abap / "src" / "zcl_mdmdoc_golden_data.clas.abap").write_text(tampered)
    # byte-equal copy -> fresh (headers + regen diff all pass)
    assert cp.golden_freshness(abap_root=fake_abap) == []
    # now tamper one emitted value below the headers
    (fake_abap / "src" / "zcl_mdmdoc_golden_data.clas.abap").write_text(
        tampered.replace("doc_class = `bank`", "doc_class = `w9`", 1))
    problems = cp.golden_freshness(abap_root=fake_abap)
    assert any("hand-edit or stale" in p for p in problems)


def test_golden_gen_hash_mismatch_flagged(tmp_path):
    _need_abap()
    fake_abap = tmp_path / "abap"
    (fake_abap / "src").mkdir(parents=True)
    src = cp.ABAP_ROOT / "src" / "zcl_mdmdoc_golden_data.clas.abap"
    stale = __import__("re").sub(r"GEN-HASH [0-9a-f]{16}", "GEN-HASH " + "0" * 16,
                                 src.read_text())
    (fake_abap / "src" / "zcl_mdmdoc_golden_data.clas.abap").write_text(stale)
    problems = cp.golden_freshness(abap_root=fake_abap, regen_diff=False)
    assert any("GEN-HASH" in p for p in problems)


def test_absent_abap_exits_nonzero_without_optin(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "ABAP_ROOT", tmp_path / "nope")
    monkeypatch.delenv("MDMDOC_PARITY_OPTIONAL", raising=False)
    assert cp.run() == 1


def test_absent_abap_optin_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "ABAP_ROOT", tmp_path / "nope")
    monkeypatch.setenv("MDMDOC_PARITY_OPTIONAL", "1")
    assert cp.run() == 0
