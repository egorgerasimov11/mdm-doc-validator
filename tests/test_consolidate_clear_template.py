"""BPTemplate.clear_data_rows — wipe existing vendors, keep structure."""
from __future__ import annotations

import openpyxl

from consolidation_helpers import cell, make_template, prefill_vendor
from mdmdoc.consolidation.template_io import BPTemplate, sheet_value_hash


def test_clear_removes_data_keeps_structure(tmp_path):
    path = prefill_vendor(make_template(tmp_path / "t.xlsx"),
                          source_id="OLD_1", name="EXISTING GMBH")
    # a second pre-existing vendor
    prefill_vendor(path, source_id="OLD_2", name="ANOTHER AG")
    before_bytes = path.read_bytes()

    tpl = BPTemplate(path)
    assert tpl.data_rows_used("LFA1 - Supplier General") == 2
    res = tpl.clear_data_rows()
    assert res["rows"] >= 2 and res["sheets"] >= 1
    # now empty
    assert tpl.data_rows_used("LFA1 - Supplier General") == 0
    assert tpl.existing_source_ids() == set()
    # header row + '! Read Me !' intact
    assert tpl.header_row("LFA1 - Supplier General") == 2
    rm = tpl.wb["! Read Me !"]
    assert rm.cell(row=1, column=1).value  # description text preserved
    # snapshot after clear: data sheets report only the header row
    snap = tpl.snapshot()
    assert snap["LFA1 - Supplier General"]["rows"] == 2
    out = tpl.save_to(tmp_path / "cleared.xlsx")
    tpl.close()

    # the uploaded file itself is never mutated
    assert path.read_bytes() == before_bytes
    # cleared output really is empty of data
    wb = openpyxl.load_workbook(out)
    assert cell(wb, "LFA1 - Supplier General", 3, "NAME1") in (None, "")
    wb.close()


def test_clear_then_append_lands_at_header_plus_one(tmp_path):
    path = prefill_vendor(make_template(tmp_path / "t.xlsx"))
    tpl = BPTemplate(path)
    tpl.clear_data_rows()
    plan = tpl.append_rows({"LFA1 - Supplier General": [
        {"SOURCE_ID": "N1", "NAME1": "FRESH LLC", "LAND1": "US"}]})
    tpl.close()
    assert min(c["row"] for c in plan) == 3  # header row 2 -> data at 3


def test_clear_is_idempotent(tmp_path):
    path = prefill_vendor(make_template(tmp_path / "t.xlsx"))
    tpl = BPTemplate(path)
    tpl.clear_data_rows()
    res2 = tpl.clear_data_rows()
    assert res2["rows"] == 0
    tpl.close()
