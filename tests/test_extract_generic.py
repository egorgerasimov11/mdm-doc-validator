"""The generic reader: any document hands the host every field it can name —
company data, contacts, tax identifiers — on top of the bank schema. Modeled on
a real German supplier self-disclosure (labels in one column, values in the
next; the bank block on the last page)."""
from __future__ import annotations

import pytest

from mdmdoc.extract import api
from mdmdoc.extract.extractor import guess_doc_type
from mdmdoc.extract.forms import generic


def _ln(text, x0, y0, x1, y1):
    return {"text": text, "bbox_pct": [x0, y0, x1, y1]}


def _page(no, rows, extra_lines=()):
    """rows: (label, value) pairs laid out two-column on the same visual row."""
    tl = []
    y = 10.0
    for label, value in rows:
        tl.append(_ln(label, 8, y, 26, y + 1.2))
        if value:
            tl.append(_ln(value, 34, y, 70, y + 1.2))
        y += 3.0
    tl += [_ln(*l) for l in extra_lines]
    text = "\n".join(l["text"] for l in tl)
    return {"page": no, "size": [1545, 2000], "lines": {"textlayer": tl},
            "readings": {"textlayer": text}, "fields": [], "transcript": text}


@pytest.fixture()
def selbstauskunft_doc():
    p0 = _page(0, [
        ("LIEFERANTENSELBSTAUSKUNFT", ""),
        ("Firma", "Andreas Maier GmbH & Co. KG"),
        ("Anschrift", "Waiblinger Straße 116, 70734 Fellbach"),
        ("Telefon", "+49 711 5766 - 0"),
        ("Telefax", "+49 711 575725"),
        ("E-Mail", "amf@amf.de"),
        ("Internet", "www.amf.de"),
        ("Unternehmensleitung", "Johannes Maier"),
        ("Handelsregister", "HRB 261588"),
        ("Umsatzsteueridentnummer", "DE147321245"),
        ("DUNS-Nr.", "31-559-4194"),
    ])
    p1, p2 = _page(1, [("Produktspektrum", "Spannsysteme")]), _page(2, [("Zertifizierung", "ISO 9001")])
    p3 = _page(3, [
        ("Bankdaten", "Commerzbank AG"),
        ("BLZ", "600 800 00"),
        ("Kontonummer", "321 167 800"),
        ("BIC", "DRESDEFF600"),
        ("IBAN", "DE20 6008 0000 0321 1678 00"),
    ])
    return {"pages": 4, "pages_out": [p0, p1, p2, p3]}


def test_doc_type_supplier_self_disclosure():
    assert guess_doc_type("LIEFERANTENSELBSTAUSKUNFT\nAllgemeine Informationen") == "supplier self-disclosure"
    assert guess_doc_type("Supplier Self-Disclosure form") == "supplier self-disclosure"
    # a self-disclosure that also names its bank stays a self-disclosure
    assert guess_doc_type("Lieferantenselbstauskunft\nBankverbindung: Commerzbank") == "supplier self-disclosure"
    assert guess_doc_type("Bankverbindung: Commerzbank AG") == "bank confirmation letter"


def test_generic_reads_company_fields(selbstauskunft_doc):
    fields, _extra = generic.read(selbstauskunft_doc, bank=({}, {}))
    got = {k: v["value"] for k, v in fields.items() if v.get("value")}
    assert got["company_name"] == "Andreas Maier GmbH & Co. KG"
    assert got["street"] == "Waiblinger Straße 116"
    assert got["postal_code"] == "70734"
    assert got["city"] == "Fellbach"
    assert got["phone"] == "+49 711 5766 - 0"
    assert got["fax"] == "+49 711 575725"
    assert got["email"] == "amf@amf.de"
    assert got["website"] == "www.amf.de"
    assert got["vat_id"] == "DE147321245"
    assert got["registration"] == "HRB 261588"
    assert got["management"] == "Johannes Maier"
    assert got["duns"] == "315594194"
    assert fields["company_name"]["page"] == 0


def test_generic_label_column_is_not_a_value():
    """The transcript of a label-column layout lists labels one under another —
    the next label must never be read as the value."""
    tl = [_ln("Firma", 8, 10, 14, 11), _ln("Anschrift", 8, 13, 16, 14), _ln("Telefon", 8, 16, 14, 17)]
    page = {"page": 0, "lines": {"textlayer": tl}, "readings": {"textlayer": "Firma\nAnschrift\nTelefon"},
            "fields": [], "transcript": ""}
    fields, _ = generic.read({"pages_out": [page]}, bank=({}, {}))
    assert not fields["company_name"]["value"]
    assert not fields["phone"]["value"]


def test_generic_keeps_bank_schema(selbstauskunft_doc):
    bank_stub = {"iban": {"value": "DE20600800000321167800", "pretty": "", "status": "checksum_ok",
                          "page": 3, "bbox_pct": None, "evidence": "", "voices": []}}
    fields, extra = generic.read(selbstauskunft_doc, bank=(bank_stub, {"ibans": []}))
    assert fields["iban"]["value"] == "DE20600800000321167800"   # bank keys pass through untouched
    assert extra == {"ibans": []}


def test_contract_is_additive():
    assert api.API_VERSION == 1
    assert "supplier self-disclosure" not in api.BANK_TYPES     # class still decided by identifiers
    caps_flag = "generic_fields"
    # the flag is set statically — read it without probing engines
    import inspect
    assert caps_flag in inspect.getsource(api.capabilities)
