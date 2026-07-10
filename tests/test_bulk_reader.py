"""V-wave: bulk reader — canonical templates, raw SE16N shapes (mirrors of the
four real exports, synthetic values only), case detection, references."""
import pytest
from openpyxl import Workbook

from mdmdoc.bulk import reader


def _wb(tmp_path, name, headers, rows, sheet="Data", preamble=0):
    p = tmp_path / name
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for _ in range(preamble):
        ws.append(["some preamble line"])
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(p)
    return p


RAW_TAX_HDRS = ["Business Partner", "Tax Number Category", "Tax number",
                "Tax Number Long", "Tax Number Registratio"]
RAW_BANK_HDRS = ["Business Partner", "Bank Details ID", "Bank Country/Region",
                 "Bank Key", "Bank acct", "Bank Control Key", "Reference Details",
                 "Account Holder Name", "Collection authorizati", "Account Name",
                 "Valid From", "Valid To"]


def test_detect_case_raw_shapes(tmp_path):
    tax = _wb(tmp_path, "tax.xlsx", RAW_TAX_HDRS, [["1", "US2", "12-3456789", "", ""]])
    bank = _wb(tmp_path, "bank.xlsx", RAW_BANK_HDRS,
               [["1", "0001", "US", "021000021", "12345678", "01", "", "ACME", "", "", "", ""]])
    region = _wb(tmp_path, "adr.xlsx",
                 ["Business Partner", "Country", "Region", "Postal Code", "City"],
                 [["1", "US", "TX", "75201", "Dallas"]])
    assert reader.detect_case(tax) == "tax"
    assert reader.detect_case(bank) == "bank"
    assert reader.detect_case(region) == "region"


def test_read_rows_raw_vs_template_kind(tmp_path):
    # the raw tax dump's headers NORMALIZE to the canonical ones ("Tax number"
    # == "Tax Number") — honestly reported as template-shaped
    raw = _wb(tmp_path, "raw.xlsx", RAW_TAX_HDRS, [["7", "DE0", "DE811907980", "", ""]])
    rows, cols, kind = reader.read_rows(raw, "tax")
    assert rows[0]["tax_number"] == "DE811907980"
    # the BUT0BK dump has genuinely different headers ("Bank acct") -> raw-export
    rawb = _wb(tmp_path, "rawb.xlsx", RAW_BANK_HDRS,
               [["1", "0001", "US", "021000021", "12345678", "01", "", "A", "", "", "", ""]])
    rows, cols, kind = reader.read_rows(rawb, "bank")
    assert kind == "raw-export" and rows[0]["bank_account"] == "12345678"
    tpl = _wb(tmp_path, "tpl.xlsx",
              ["Business Partner", "Tax Number Category", "Tax Number",
               "Tax Number Long", "Country"],
              [["7", "DE0", "DE811907980", "", "DE"]])
    rows, cols, kind = reader.read_rows(tpl, "tax")
    assert kind == "template" and rows[0]["country"] == "DE"


def test_header_row_found_past_preamble(tmp_path):
    p = _wb(tmp_path, "pre.xlsx", RAW_TAX_HDRS,
            [["9", "IT0", "00743110157", "", ""]], preamble=3)
    rows, cols, kind = reader.read_rows(p, "tax")
    assert len(rows) == 1 and rows[0]["partner"] == "9"


def test_unrecognizable_raises(tmp_path):
    p = _wb(tmp_path, "junk.xlsx", ["Foo", "Bar"], [["1", "2"]])
    with pytest.raises(reader.BulkInputError):
        reader.detect_case(p)
    with pytest.raises(reader.BulkInputError):
        reader.read_rows(p, "tax")


def test_t005s_and_t005u_readers(tmp_path):
    s = _wb(tmp_path, "t005s.xlsx",
            ["Country/Region Key", "Region", "Provincial Tax Code",
             "State of manufacture", "Description"],
            [["US", "TX", "", "", "Texas"], ["US", "CA", "", "", "California"],
             ["TH", "98", "", "", "Bangkok"], ["", "", "", "", "Foreign"]])
    regions = reader.read_t005s(s)
    assert regions["US"]["TX"] == "Texas" and regions["TH"]["98"] == "Bangkok"
    u = _wb(tmp_path, "t005u.xlsx",
            ["Language Key", "Country/Region Key", "Region", "Description"],
            [["EN", "US", "TX", "Texas"], ["ZH", "US", "TX", "得克萨斯州"]])
    texts = reader.read_t005u(u)
    assert texts[("EN", "US")]["TX"] == "Texas"
    assert texts[("ZH", "US")]["TX"] == "得克萨斯州"
    with pytest.raises(reader.BulkInputError):
        reader.read_t005s(_wb(tmp_path, "x.xlsx", ["A", "B"], [["1", "2"]]))
