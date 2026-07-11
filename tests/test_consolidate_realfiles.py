"""Optional E2E smoke against the REAL files (never committed, env-gated):

    MDMDOC_CONSOL_REAL_FORM=".../Americas MacroEnabled MDM_SETH_DEVRIES_HCP (2).xlsm" \\
    MDMDOC_CONSOL_REAL_TEMPLATE=".../BusinessPartnerTemplate.xlsx" \\
    uv run --with-editable <sap-vendor-autoload> pytest tests/test_consolidate_realfiles.py

Expected values are read from the form itself — nothing sensitive is baked in.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from consolidation_helpers import needs_converter

pytestmark = [
    needs_converter,
    pytest.mark.skipif(
        not (os.environ.get("MDMDOC_CONSOL_REAL_FORM")
             and os.environ.get("MDMDOC_CONSOL_REAL_TEMPLATE")),
        reason="set MDMDOC_CONSOL_REAL_FORM / MDMDOC_CONSOL_REAL_TEMPLATE"),
]


def test_real_form_full_cycle(tmp_path):
    from mdmdoc.consolidation import convert, plan as planmod
    from mdmdoc.consolidation import source_id as sidmod
    from mdmdoc.consolidation.template_io import BPTemplate
    from mdmdoc.consolidation.verify import verify_output

    form = Path(os.environ["MDMDOC_CONSOL_REAL_FORM"])
    template = Path(os.environ["MDMDOC_CONSOL_REAL_TEMPLATE"])

    tpl = BPTemplate(template)
    tpl.validate()
    sid = sidmod.generate(tpl.existing_source_ids())
    built = convert.build_vendor_rows(form, tpl, source_id=sid)
    assert built["errors"] == []

    cells = tpl.plan_rows(built["rows"])
    review = planmod.review(cells, kind="form", source_id=sid)
    assert review["errors"] == []

    pre = tpl.snapshot()
    wp = tpl.append_rows(built["rows"])
    out = tpl.save_to(tmp_path / "real_out.xlsx")
    tpl.close()

    out_tpl = BPTemplate(out)
    fresh = convert.build_vendor_rows(form, out_tpl, source_id=sid)
    out_tpl.close()
    report = verify_output(out, wp, pre, {sid: fresh["rows"]})
    assert report["status"] == "verified", [
        e for p in report["passes"] for e in p["errors"]]

    # spot checks against the form's own values
    from sap_vendor_autoload.reader import SourceForm
    src = SourceForm(form)
    try:
        name = src.get_str("2. Vendor Details", "E15")
        country_raw = src.get_str("2. Vendor Details", "E25")
    finally:
        src.close()
    lfa1 = built["rows"]["LFA1 - Supplier General"][0]
    assert lfa1["NAME1"] == name
    if country_raw and country_raw.upper() in ("USA", "US"):
        assert lfa1["LAND1"] == "US"

    # coverage: no extracted field silently lost
    extract = convert.extract_form(form)
    cov = convert.coverage(extract["fields"], built["rows"], built["unmapped"])
    assert [c for c in cov if c["status"] == "not_loaded"] == []
