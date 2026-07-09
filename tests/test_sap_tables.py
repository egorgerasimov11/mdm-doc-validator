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


def _cmp(doc_fields: dict, sap_fields: dict):
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = doc_fields
    return sap_compare.compare(ext, sap_fields)


def test_account_midstring_containment_is_mismatch():
    """audit-wave C9: an unanchored substring hit inside the SAP IBAN used to
    declare 'match' and silence SAP-003 even though SAP's account differed."""
    findings, crows = _cmp(
        {"account_number": "123456"},
        {"bank_account": "999999", "iban": "DE44500105123456777918"})  # mid-string
    assert any(f.rule_id == "SAP-003" for f in findings)
    assert any(r["field"] == "Bank Account" and r["status"] == "MISMATCH" for r in crows)


def test_account_iban_tail_consistent_decomposition_matches():
    # doc account and SAP BANKN both anchor the same IBAN tail (branch-code
    # prefix differs) — the normal decomposed-IBAN shape: match, no finding
    findings, crows = _cmp(
        {"account_number": "40378412"},
        {"bank_account": "000040378412", "iban": "IT39T0200801671000040378412"})
    # zero-strip equality already matches this; force the tail path with a
    # branch-prefixed BANKN instead
    findings, crows = _cmp(
        {"account_number": "40378412"},
        {"bank_account": "01671000040378412", "iban": "IT39T0200801671000040378412"})
    ids = {f.rule_id for f in findings}
    assert "SAP-003" not in ids and "SAP-009" not in ids
    assert any(r["field"] == "Bank Account" and r["status"] == "match"
               and "decomposition" in r.get("note", "") for r in crows)


def test_account_iban_tail_but_bankn_inconsistent_warns_sap009():
    # doc matches the IBAN tail, but SAP's stored BANKN is NOT consistent with
    # SAP's own IBAN — not a silent match (old bug), not a hard SAP-003 either
    findings, crows = _cmp(
        {"account_number": "40378412"},
        {"bank_account": "555555", "iban": "IT39T0200801671000040378412"})
    ids = {f.rule_id for f in findings}
    assert "SAP-009" in ids and "SAP-003" not in ids
    assert any(r["field"] == "Bank Account" and r["status"] == "MISMATCH" for r in crows)


def test_account_short_tail_falls_to_mismatch():
    # <6 significant digits must not anchor (check digits/branch codes collide)
    findings, _ = _cmp(
        {"account_number": "78412"},
        {"bank_account": "999999", "iban": "IT39T0200801671000040378412"})
    assert any(f.rule_id == "SAP-003" for f in findings)


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


def test_but000_with_trailing_bank_columns_not_but0bk(tmp_path):
    # real BUT000 SE16N dumps carry a trailing Bank Country/Bank Key pair — that
    # must NOT make them read as BUT0BK (regression from the real export)
    headers = ["Business Partner", "Partner Cat.", "Name 1", "Full Name",
               "Central Block", "Natural Person", "Bank Country/Region", "Bank Key"]
    p = _xlsx(tmp_path, "g.xlsx", headers,
              [["29653", "2", "ACME LLC", "ACME LLC", "", "", "", ""]])
    kind, _ = sap_tables.load(p)
    assert kind == "BUT000"


def test_unknown_sheet_raises(tmp_path):
    p = _xlsx(tmp_path, "x.xlsx", ["Foo", "Bar", "Baz"], [["1", "2", "3"]])
    with pytest.raises(sap_tables.SapTableError):
        sap_tables.load(p)
