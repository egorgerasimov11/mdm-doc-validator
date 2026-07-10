"""P5: cross-page corroboration — an ID printed on >=2 pages of DIFFERENT
page classes upgrades a model-only read to independently confirmed."""
from mdmdoc import confidence, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

IBAN = "DE89370400440532013000"

LETTER = ("This letter is to confirm the account details below. "
          "Account confirmation for Fake Corp GmbH. "
          f"IBAN: DE89 3704 0044 0532 0130 00")
SHEET = ("Supplier banking sheet\nBeneficiary: Fake Corp GmbH\n"
         f"IBAN {IBAN}\nCurrency EUR")


def _raw(pages: dict) -> RawDoc:
    r = RawDoc(path="x.pdf", sha256="b" * 16, ext=".pdf", doc_class="bank")
    r.page_texts = dict(pages)
    r.pages_used = sorted(pages)
    return r


def _ext(**fields) -> Extraction:
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"iban": IBAN, **fields}
    e.provenance["iban"] = {"source": "model", "page": None}
    return e


def test_two_page_classes_confirm_independently():
    ext = _ext()
    stage_b._corroborate_across_pages(ext, _raw({0: LETTER, 1: SHEET}))
    assert ext.provenance["iban"]["confirmed_independent"] is True
    note = next(n for n in ext.crosscheck if n.startswith("iban="))
    assert "bank_letter" in note and "plain" in note


def test_same_class_pages_do_not_confirm():
    ext = _ext()
    stage_b._corroborate_across_pages(ext, _raw({0: LETTER, 1: LETTER + " p2"}))
    assert "confirmed_independent" not in ext.provenance["iban"]
    assert not ext.crosscheck


def test_single_page_never_confirms():
    ext = _ext()
    stage_b._corroborate_across_pages(ext, _raw({0: LETTER}))
    assert "confirmed_independent" not in ext.provenance["iban"]


def test_note_carries_no_id_digits():
    ext = _ext()
    stage_b._corroborate_across_pages(ext, _raw({0: LETTER, 1: SHEET}))
    note = next(n for n in ext.crosscheck if n.startswith("iban="))
    assert "532013" not in note and IBAN not in note


def test_confidence_model_only_weak_is_settled():
    ext = _ext()
    raw = _raw({0: LETTER, 1: SHEET})
    base = confidence.assess(ext)
    assert any("model-only" in r for r in base["reasons"])
    stage_b._corroborate_across_pages(ext, raw)
    after = confidence.assess(ext)
    assert not any("model-only" in r for r in after["reasons"])
