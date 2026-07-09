"""T: SAP table-export comparison (BUT0BK bank details / BUT000 BP general).
Header fingerprint (descriptive + technical), row selection, reverse-lookup,
IBAN decomposition through the reused compare(), and BUT000 name/category."""
import openpyxl
import pytest

from mdmdoc import sap_compare, sap_tables
from mdmdoc.fields import Extraction


def _xlsx(tmp_path, name, headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(headers)
    for r in rows:
        ws.append(r)
    p = tmp_path / name
    wb.save(p)
    return p


# --- BUT0BK ---------------------------------------------------------------
BUT0BK_DESC = ["Business Partner", "Bank Details ID", "Bank Country/Region",
               "Bank Key", "Bank acct", "Bank Control Key", "Reference Details",
               "Account Holder Name", "IBAN"]
BUT0BK_TECH = ["PARTNER", "BKVID", "BANKS", "BANKL", "BANKN", "BKONT", "BKREF",
               "KOINH", "IBAN"]
# IT: IBAN IT60 X 05428 11101 000000123456 → ABI+CAB = 0542811101 = iban[5:15]
IT_IBAN = "IT60X0542811101000000123456"


def test_detect_and_map_descriptive(tmp_path):
    p = _xlsx(tmp_path, "b.xlsx", BUT0BK_DESC,
              [["50000111", "0001", "IT", "0542811101", "000000123456",
                "", "", "ACME S.p.A.", ""]])
    kind, rows = sap_tables.load(p)
    assert kind == "BUT0BK" and len(rows) == 1
    sf = sap_tables.to_sap_fields(rows[0])
    assert sf["bank_country"] == "IT" and sf["bank_key"] == "0542811101"
    assert sf["bank_account"] == "000000123456" and sf["account_holder"] == "ACME S.p.A."


def test_detect_technical_headers(tmp_path):
    p = _xlsx(tmp_path, "b.xlsx", BUT0BK_TECH,
              [["50000111", "0001", "IT", "0542811101", "000000123456", "", "",
                "ACME", IT_IBAN]])
    kind, rows = sap_tables.load(p)
    assert kind == "BUT0BK"


def test_iban_decomposition_confirms_through_compare(tmp_path):
    # SAP row: decomposed IT IBAN (BANKL + BANKN, IBAN column EMPTY); document
    # carries the full IBAN → compare must CONFIRM, not warn about a missing IBAN.
    p = _xlsx(tmp_path, "b.xlsx", BUT0BK_TECH,
              [["50000111", "0001", "IT", "0542811101", "000000123456", "", "",
                "ACME", ""]])
    _, rows = sap_tables.load(p)
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"iban": IT_IBAN, "bank_country": "IT"}
    row, sel, _ = sap_tables.select_row(rows, ext, "50000111")
    assert row is not None
    findings, crows = sap_compare.compare(ext, sap_tables.to_sap_fields(row))
    ids = {f.rule_id for f in findings}
    assert "SAP-002" not in ids   # no false "IBAN only on one side"
    assert "SAP-006" not in ids   # IT bank-key window fixed
    assert any(r["field"] == "IBAN" and r["status"] == "match" for r in crows)


def test_row_selection_and_multi(tmp_path):
    p = _xlsx(tmp_path, "b.xlsx", BUT0BK_TECH, [
        ["50000111", "0001", "IT", "0542811101", "000000123456", "", "", "ACME", ""],
        ["50000111", "0002", "DE", "37040044", "0532013000", "", "", "ACME", ""],
    ])
    _, rows = sap_tables.load(p)
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"iban": IT_IBAN}
    row, findings, _ = sap_tables.select_row(rows, ext, "50000111")
    assert row["BKVID"] == "0001"          # best match by IBAN, not first row
    assert any(f.rule_id == "SAP-011" for f in findings)   # "N details on file"


def test_reverse_lookup_notes_partner_only(tmp_path):
    p = _xlsx(tmp_path, "b.xlsx", BUT0BK_TECH,
              [["50000999", "0001", "IT", "0542811101", "000000123456", "", "",
                "ACME", IT_IBAN]])
    _, rows = sap_tables.load(p)
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"iban": IT_IBAN}
    row, findings, partners = sap_tables.select_row(rows, ext, "")
    assert partners == ["50000999"]
    msg = findings[0].message
    assert "50000999" in msg and "123456" not in msg   # partner id, never digits


def test_us_aba_leading_zero_preserved_as_text(tmp_path):
    # a text cell keeps the leading zero (SE16 export shape); numeric cell can't
    p = _xlsx(tmp_path, "b.xlsx", BUT0BK_TECH,
              [["50000111", "0001", "US", "011000015", "12345678", "", "", "ACME", ""]])
    _, rows = sap_tables.load(p)
    assert sap_tables.to_sap_fields(rows[0])["bank_key"] == "011000015"


# --- BUT000 ---------------------------------------------------------------
BUT000_TECH = ["PARTNER", "TYPE", "NAME_ORG1", "BU_FULLNAME", "XBLCK", "NATPERS"]


def test_but000_name_and_category(tmp_path):
    p = _xlsx(tmp_path, "g.xlsx", BUT000_TECH,
              [["29653", "2", "China Med Device LLC", "China Med Device LLC", "", ""]])
    kind, rows = sap_tables.load(p)
    assert kind == "BUT000"
    ext = Extraction(doc_class="w9", doc_type="w9")
    ext.fields = {"line1_name": "China Med Device, LLC", "line3_classification": "LLC"}
    findings, crows = sap_tables.compare_bp(ext, rows[0], policy="masked")
    ids = {f.rule_id for f in findings}
    assert "SAP-030" not in ids   # names materially equal
    assert any(r["field"].startswith("Name") and r["status"] == "match" for r in crows)


def test_but000_central_block_warns(tmp_path):
    p = _xlsx(tmp_path, "g.xlsx", BUT000_TECH,
              [["29653", "2", "ACME LLC", "ACME LLC", "X", ""]])
    _, rows = sap_tables.load(p)
    ext = Extraction(doc_class="w9", doc_type="w9")
    ext.fields = {"line1_name": "ACME LLC"}
    findings, _ = sap_tables.compare_bp(ext, rows[0])
    assert any(f.rule_id == "SAP-032" for f in findings)


def test_unknown_sheet_raises(tmp_path):
    p = _xlsx(tmp_path, "x.xlsx", ["Foo", "Bar", "Baz"], [["1", "2", "3"]])
    with pytest.raises(sap_tables.SapTableError):
        sap_tables.load(p)
