"""Cases from the Doc Lab bench (2026-09-25)."""
from mdmdoc.extract.extractor import _pretty_span
from mdmdoc.extract.forms import bank, generic
from mdmdoc.extract.forms.common import norm_text


def test_pretty_span_survives_ligature():
    # Needham wire form: "ﬁ".upper() == "FI" shifted the flat string → IndexError
    pretty, start = _pretty_span("Beneﬁciary Account 123456789", "ACCOUNT123456789")
    assert pretty == "Account 123456789" and start == 11


def _pl_doc(extra_line=""):
    text = ("Posiadacz rachunku SZPITAL WOJEWÓDZKI\n"
            "Numer rachunku 08 1130 1222 0030 2002 6720 0003\n"
            "Numer rachunku VAT 42 1130 1222 0030 2002 6760 0001\n" + extra_line)
    tess = text.replace("Ó", "O")
    return {"pages_out": [{"page": 0, "readings": {"rapidocr:auto": text, "tess:eng": tess}, "lines": {},
                           "fields": [{"kind": "tax id", "value": "0001", "label": "Numer rachunku VAT 42 1130",
                                       "status": "confirmed", "voices": ["rapidocr:auto", "tess:eng"]}]}]}


def test_polish_nrb_is_the_iban_and_vat_account_is_set_apart():
    fields, extra = bank.read(_pl_doc())
    assert fields["iban"]["value"] == "PL08113012220030200267200003"
    assert fields["iban"]["status"] == "confirmed"
    assert [v["value"] for v in extra["vat_accounts"]] == ["PL42113012220030200267600001"]
    assert fields["account_holder"]["value"].startswith("SZPITAL")
    assert fields["account_holder"]["status"] == "confirmed"      # Ó vs O is one reading


def test_vat_account_digits_are_not_a_vat_id():
    doc = _pl_doc()
    fields, _ = generic.read(doc, bank=bank.read(doc))
    assert fields["vat_id"]["value"] == ""


def test_norm_text_folds_latin_diacritics_only():
    assert norm_text("Wojewódzki") == norm_text("WOJEWODZKI")
    assert norm_text("한국어 ガ") == "한국어 ガ"
