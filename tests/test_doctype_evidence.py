"""П3/R1: evidence-based doc_type rescue. Strong deterministic bank-letter
evidence rescues an ungrounded model payment_instructions call into
bank_letter+uncertain; anything weaker keeps the conservative other→NMR
grounding; true payment instructions are untouched."""
import pytest

from mdmdoc import confidence
from mdmdoc.doctype_evidence import score
from mdmdoc.fields import Extraction
from mdmdoc.stage_b import _ground_payment_instructions

ZAJECKA = """AS Swedbank
Bank confirmation letter
To whom it may concern,
We confirm that the account of John M. Zajecka is maintained at our bank.
IBAN: LV97HABA0012345678910
This letter is issued at the client's request."""

CJK_NOTICE = """2026 医疗器械展览会 招商通知
参展费用请汇入以下账户
开户银行: 某某银行
账号: 1234567890123
联系人: 王先生"""

TRUE_PI = """Supplier payment instructions
Please remit all future payments per the standard settlement instructions below.
Beneficiary: Acme LLC
IBAN: DE89370400440532013000"""


def _ext(doc_type="payment_instructions"):
    return Extraction(doc_class="bank", doc_type=doc_type)


def _raw(text, cands=None, letter_pages=None):
    from types import SimpleNamespace
    return SimpleNamespace(raw_text=text, regex_candidates=cands or {},
                           bank_letter_pages=letter_pages or [])


def test_zajecka_letter_rescued():
    ext = _ext()
    _ground_payment_instructions(ext, _raw(ZAJECKA, {"iban": "LV97HABA0012345678910"}))
    assert ext.doc_type == "bank_letter"
    assert ext.doc_type_uncertain is True
    assert ext.doc_type_evidence["letter_shape"] is True
    assert ext.provenance["doc_type"]["source"] == "rule"
    assert any("classified bank_letter (uncertain)" in w for w in ext.warnings)


def test_cjk_conference_notice_stays_other():
    ext = _ext()
    _ground_payment_instructions(ext, _raw(CJK_NOTICE, {"account_number": "1234567890123"}))
    assert ext.doc_type == "other"                    # byte-identical to before
    assert ext.doc_type_uncertain is False


def test_true_payment_instructions_unchanged():
    ext = _ext()
    _ground_payment_instructions(ext, _raw(TRUE_PI, {"iban": "DE89370400440532013000"}))
    assert ext.doc_type == "payment_instructions"     # marks present -> early exit
    assert ext.doc_type_uncertain is False


@pytest.mark.parametrize("drop", ["letter_shape", "bank_identity",
                                  "account_facts", "holder_signal"])
def test_rescue_requires_every_component(drop):
    text = ZAJECKA
    cands = {"iban": "LV97HABA0012345678910"}
    if drop == "letter_shape":
        text = text.replace("Bank confirmation letter", "Notice") \
                   .replace("We confirm that", "Regarding") \
                   .replace("is maintained at our bank", "is with us") \
                   .replace("To whom it may concern,", "")
    elif drop == "bank_identity":
        text = text.replace("AS Swedbank", "AS Swed").replace("at our bank", "with us")
    elif drop == "account_facts":
        cands = {}
    elif drop == "holder_signal":
        text = text.replace("To whom it may concern,", "") \
                   .replace("We confirm that the account of", "The number for") \
                   .replace("is maintained at our bank", "follows")
    sc = score(text, cands, [])
    assert sc["components"][drop] is False, f"setup failed to drop {drop}"
    ext = _ext()
    _ground_payment_instructions(ext, _raw(text, cands))
    assert ext.doc_type == "other", f"rescue must NOT fire without {drop}"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("MDMDOC_DOCTYPE_RESCUE", "0")
    ext = _ext()
    _ground_payment_instructions(ext, _raw(ZAJECKA, {"iban": "LV97HABA0012345678910"}))
    assert ext.doc_type == "other"                    # blunt grounding restored


def test_confidence_weak_signal():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"iban": "LV97HABA0012345678910", "account_holder": "John M. Zajecka"}
    e.provenance = {"iban": {"source": "ocr-regex", "confirmed": True}}
    e.doc_type_uncertain = True
    a = confidence.assess(e)
    assert a["level"] == "medium"
    assert any("doc_type rescued" in r for r in a["reasons"])
    e.signature_probe = {"uncertain": True}           # second weak -> low
    assert confidence.assess(e)["level"] == "low"


def test_to_public_emits_evidence_only_when_set():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    assert "doc_type_uncertain" not in e.to_public()
    e.doc_type_uncertain = True
    e.doc_type_evidence = {"letter_shape": True}
    pub = e.to_public()
    assert pub["doc_type_uncertain"] is True
    assert pub["doc_type_evidence"] == {"letter_shape": True}
