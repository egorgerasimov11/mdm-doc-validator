"""The drift detector's pure parsers must stay correct, and the manifest must
list exactly the predicates Python implements (so a new predicate can't be added
without a conscious parity entry). ABAP-repo-dependent checks are exercised only
when the ABAP checkout is present."""
import sys
from pathlib import Path

import pytest

PY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PY_ROOT))  # make the repo-root `tools/` package importable

from tools import check_parity as cp  # noqa: E402

# the canonical predicate set — if this changes, both ABAP and PARITY.md must follow
PREDICATES = {"unsigned_no_evidence", "unsigned_typed_block", "field_empty",
              "no_bank_ids", "swift_valid", "iban_valid", "ein_shape",
              "tin_type_vs_classification", "individual_with_business_name_and_ein",
              "line_swap_suspect", "date_older_than"}


def test_python_predicates_match_registry():
    assert cp.python_predicates(PY_ROOT) == PREDICATES


def test_manifest_lists_exactly_the_predicates():
    listed, _pending = cp.parity_manifest(PY_ROOT)
    assert listed == PREDICATES, "PARITY.md predicate list drifted from REGISTRY"


def test_yaml_rules_and_checks_parse():
    rules = cp.yaml_rules(PY_ROOT / "rules" / "banking.yaml")
    assert "BNK-001" in rules and "__tables__" in rules
    used = cp.yaml_checks_used(PY_ROOT / "rules" / "banking.yaml")
    assert "iban_valid" in used and used <= PREDICATES


@pytest.mark.skipif(not cp.ABAP_ROOT.exists(), reason="ABAP repo not checked out")
def test_abap_dispatch_matches_python_predicates():
    # the ABAP rule engine must dispatch exactly the predicates Python implements
    assert cp.abap_check_names() == PREDICATES


@pytest.mark.skipif(not cp.ABAP_ROOT.exists(), reason="ABAP repo not checked out")
def test_rule_data_identical_between_repos():
    for cls in ("banking", "w9"):
        py = cp.yaml_rules(PY_ROOT / "rules" / f"{cls}.yaml")
        abap = cp.yaml_rules(cp.ABAP_ROOT / "rules" / f"{cls}.yaml")
        assert py == abap, f"{cls}.yaml differs between Python and ABAP repos"
