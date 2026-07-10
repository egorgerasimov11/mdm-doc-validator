"""O1: national clearing codes (CN CNAPS 联行号, IN IFSC, UK sort, AU BSB,
DE BLZ) — label-anchored capture, the derived field, the audit relocation and
the SAP bank-key match. Real case: a Chinese letter's 联行号 303100000397 was
invisible to every field."""
from mdmdoc import ocr, report, sap_compare, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

CN_TEXT = ("银行账户信息\n开户银行名称:中国光大银行股份有限公司北京京广桥支行\n"
           "账号: 35310188000042676\n联行号: 303100000397\n")


def test_ocr_captures_five_clearing_shapes():
    cases = [
        (CN_TEXT, "303100000397", "CNAPS"),
        ("Sort Code: 61-26-30  Account No: 12345678", "612630", "SORT"),
        ("IFSC: HDFC0001234 Account 5011 2233 4455", "HDFC0001234", "IFSC"),
        ("BSB 062-000 account 1234 5678", "062000", "BSB"),
        ("Bankleitzahl: 37040044 Kontonummer: 532013000", "37040044", "BLZ"),
    ]
    for text, value, kind in cases:
        out = ocr.regex_fields(text)
        assert out.get("national_clearing") == value, text
        assert out.get("national_clearing_kind") == kind


def test_clearing_never_becomes_account():
    out = ocr.regex_fields(CN_TEXT)
    assert out["account_number"] == "35310188000042676"
    out2 = ocr.regex_fields("联行号: 303100000397\nsome text")
    assert out2.get("account_number") != "303100000397"


def _raw(text=CN_TEXT):
    r = RawDoc(path="cn.pdf", sha256="c" * 16, ext=".pdf", doc_class="bank")
    r.raw_text = text
    r.regex_candidates = ocr.regex_fields(text)
    r.type_hint = "payment_instructions"
    return r


def test_guard_fills_derived_field_deterministically():
    ext = stage_b.extract(_raw(), engine="deterministic")
    assert ext.fields["national_clearing"] == "303100000397"
    assert ext.fields["national_clearing_kind"] == "CNAPS"
    assert ext.provenance["national_clearing"]["source"] == "ocr-regex"
    assert any("national clearing code (CNAPS)" in n for n in ext.crosscheck)


def test_audit_relocates_model_misplaced_clearing():
    """Model pressed the CNAPS into routing_aba: relocate, don't demote."""
    ext = stage_b.extract(_raw(), engine="deterministic",
                          injected_llm={"routing_aba": "303100000397"})
    assert ext.fields["routing_aba"] == ""
    assert ext.fields["national_clearing"] == "303100000397"
    assert not any("removed from" in n for n in ext.crosscheck)


def test_public_view_masks_clearing():
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"national_clearing": "303100000397",
                  "national_clearing_kind": "CNAPS"}
    pub = ext.to_public(policy="masked")["fields"]["national_clearing"]
    assert pub["present"] is True and "value" not in pub
    assert pub["masked"].endswith("0397") and "30310000" not in pub["masked"]
    full = ext.to_public(policy="full")["fields"]["national_clearing"]
    assert full["value"] == "303100000397"


def test_sap_bank_key_matches_cnaps():
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_holder": "上海市对外服务北京有限公司",
                  "bank_name": "中国光大银行", "bank_country": "CN",
                  "account_number": "35310188000042676",
                  "national_clearing": "303100000397",
                  "national_clearing_kind": "CNAPS", "signed": True}
    ext.register_secrets()
    findings, rows = sap_compare.compare(ext, {
        "bank_key": "303100000397", "bank_account": "35310188000042676",
        "bank_country": "CN", "account_holder": "上海市对外服务北京有限公司"})
    key_row = next(r for r in rows if r["field"] == "Bank Key")
    assert key_row["status"] == "match"
    assert "CNAPS" in key_row["note"]
    assert not any(f.rule_id == "SAP-006" for f in findings)


def test_report_row_shows_kind():
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"national_clearing": "303100000397",
                  "national_clearing_kind": "CNAPS"}
    pub = ext.to_public(policy="full")
    row = next(r for r in report._data_rows(pub) if r[3] == "national_clearing")
    assert "303100000397" in row[1] and "(CNAPS)" in row[1]
