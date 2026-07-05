"""Deterministic-layer fixes from the 2026-07-04 operator/Codex audit:
ABI/CAB are not ABA, postal codes are not accounts, DocuSign is a signature,
IBAN checksum is an explicit audit fact, statements are their own doc type."""
from types import SimpleNamespace

from mdmdoc.fields import Extraction, iban_mod97_ok, type_hint
from mdmdoc.stage_b import _audit_bank_ids, _esignature_guard, _fix_jp_form


def _raw(text: str):
    return SimpleNamespace(raw_text=text)


def test_iban_mod97():
    assert iban_mod97_ok("IT39T0200801671000040378412")
    assert iban_mod97_ok("DE44500105175407324931")
    assert not iban_mod97_ok("DE44500105175407324930")  # one digit off
    assert not iban_mod97_ok("")


def test_audit_bank_ids_unmaps_abi_cab():
    ext = Extraction(doc_class="bank", fields={
        "iban": "IT39T0200801671000040378412", "routing_aba": "02008",
        "routing_aba_wires": "01671", "account_number": "000040378412",
        "branch_code": ""})
    _audit_bank_ids(ext, _raw("Conto corrente n. 40378412 ..."))
    assert ext.fields["routing_aba"] == "" and ext.fields["routing_aba_wires"] == ""
    assert ext.fields["branch_code"] == "01671"          # CAB doubles as branch
    assert ext.fields["account_number"] == "40378412"    # printed conto, not IBAN part
    notes = " | ".join(ext.crosscheck)
    assert "mod-97): valid" in notes
    assert "ABI/CAB" in notes and "match the IBAN structure" in notes
    # idempotent on the post-merge second pass
    before = list(ext.crosscheck)
    _audit_bank_ids(ext, _raw("Conto corrente n. 40378412 ..."))
    assert ext.crosscheck == before


def test_jp_postal_code_is_not_an_account():
    ext = Extraction(doc_class="bank", fields={
        "account_number": "8130044", "account_type": "", "bank_country": "",
        "branch_code": ""})
    _fix_jp_form(ext, _raw("Home Address: 〒813-0044 福岡県...\n"
                           "口座番号: 1442667\n支店番号: 258\n普通口座"))
    assert ext.fields["account_number"] == "1442667"
    assert ext.fields["branch_code"] == "258"
    assert "普通口座" in ext.fields["account_type"]
    assert ext.fields["bank_country"] == "JP"
    assert any("postal code" in w for w in ext.warnings)


def test_docusign_counts_as_electronic_signature():
    ext = Extraction(doc_class="bank",
                     fields={"signed": False, "signature_evidence": "", "doc_date": ""})
    _esignature_guard(ext, _raw("DocuSign Envelope ID: 5F2...\nYusuke Takaki\n"
                                "2026-07-01 | 08:18 BST"))
    assert ext.fields["signed"] is True
    assert "electronically signed" in ext.fields["signature_evidence"]
    assert ext.fields["doc_date"] == "2026-07-01"


def test_us_numeric_iban_field_is_account_not_malformed():
    # US "IBAN account no." holds a plain number — not a malformed IBAN.
    # account_number empty -> value relocated there; iban cleared.
    ext = Extraction(doc_class="bank", fields={
        "iban": "591564501132927", "account_number": "", "bank_country": "US"})
    _audit_bank_ids(ext, _raw("IBAN account no. 591564501132927 ... US"))
    assert ext.fields["iban"] == ""
    assert ext.fields["account_number"] == "591564501132927"
    assert any("this country has no IBAN" in n for n in ext.crosscheck)


def test_us_numeric_iban_duplicate_of_account_is_cleared():
    ext = Extraction(doc_class="bank", fields={
        "iban": "591564501132927", "account_number": "591564501132927",
        "bank_country": "US"})
    _audit_bank_ids(ext, _raw("IBAN account no. 591564501132927"))
    assert ext.fields["iban"] == ""
    assert ext.fields["account_number"] == "591564501132927"


def test_statement_type_hint():
    text = ("DBS Account Statement\nStatement period: 01-Jun-2026 to 30-Jun-2026\n"
            "Balance brought forward ...")
    assert type_hint("IQH Labs_DBS June 2026 statement.pdf", text, ".pdf", "bank") \
        == "bank_statement"
