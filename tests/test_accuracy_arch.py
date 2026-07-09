"""F2/F3/F4: asymmetric verdict metrics + Wilson CI, 3-state signature votes,
and the deterministic confidence gate (abstain a low-confidence ACCEPT to NMR)."""
from mdmdoc import confidence
from mdmdoc.evalrun import verdict_direction, verdict_metrics, _wilson
from mdmdoc.fields import Extraction


# --- F2: cost-weighted verdict metrics -------------------------------------
def test_verdict_direction():
    assert verdict_direction("ACCEPT", "REJECT") == "unsafe"   # softer than gold
    assert verdict_direction("REJECT", "ACCEPT") == "safe"     # stricter
    assert verdict_direction("ACCEPT", "ACCEPT") == ""
    assert verdict_direction("WARNING", "NEED_MANUAL_REVIEW") == "unsafe"


def test_verdict_metrics_asymmetry():
    # one unsafe (ACCEPT vs REJECT, gap 3) + one safe (REJECT vs WARNING, gap 2)
    m = verdict_metrics([("ACCEPT", "ACCEPT"), ("ACCEPT", "REJECT"), ("REJECT", "WARNING")])
    assert m["verdict_accuracy"] == round(1 / 3, 3)
    assert m["unsafe_error_rate"] == round(1 / 3, 3)
    assert m["safe_disagreement_rate"] == round(1 / 3, 3)
    # cost = (3*3 + 1*2) / 3 = 11/3
    assert m["verdict_cost"] == round(11 / 3, 3)
    assert len(m["verdict_accuracy_ci"]) == 2


def test_wilson_bounds():
    lo, hi = _wilson(9, 10)
    assert 0.0 <= lo <= 0.9 <= hi <= 1.0
    assert _wilson(0, 0) == [0.0, 0.0]


# --- F4: confidence gate ----------------------------------------------------
def _bank_ext():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"iban": "DE89370400440532013000", "account_holder": "Acme"}
    e.provenance = {"iban": {"source": "ocr-regex", "confirmed": True}}
    return e


def test_confidence_high_when_clean():
    assert confidence.assess(_bank_ext())["level"] == "high"


def test_confidence_low_on_id_mismatch():
    e = _bank_ext()
    e.crosscheck = ["iban=MISMATCH(model=DE.. vs regex=DE..)"]
    a = confidence.assess(e)
    assert a["level"] == "low" and any("disagree on iban" in r for r in a["reasons"])


def test_confidence_low_on_engine_disagreement():
    e = _bank_ext()
    e.engine_compare = [{"field": "account_number", "agree": False}]
    assert confidence.assess(e)["level"] == "low"


def test_confidence_medium_on_single_weak_signal():
    e = _bank_ext()
    e.fields["swift_bic"] = "COBADEFF"
    e.provenance["swift_bic"] = {"source": "model"}   # model-only, unconfirmed
    assert confidence.assess(e)["level"] == "medium"


def test_confidence_two_weak_is_low():
    e = _bank_ext()
    e.fields["swift_bic"] = "COBADEFF"
    e.provenance["swift_bic"] = {"source": "model"}
    e.signature_probe = {"uncertain": True}
    assert confidence.assess(e)["level"] == "low"
