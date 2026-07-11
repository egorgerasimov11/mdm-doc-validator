"""merge.py — form base + document-authoritative bank fields, candidate quiz."""
from __future__ import annotations

import openpyxl
import pytest

from consolidation_helpers import (
    bank_run_fields,
    make_americas_form,
    make_template,
    needs_converter,
    write_run,
)
from mdmdoc import config

pytestmark = needs_converter


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setenv("MDMDOC_BANK_VALUES", "full")
    monkeypatch.setenv("MDMDOC_TIN_VALUES", "full")
    return tmp_path


def _tpl(tmp_path):
    from mdmdoc.consolidation.template_io import BPTemplate
    t = BPTemplate(make_template(tmp_path / "tpl.xlsx"))
    t.validate()
    return t


def test_two_routings_are_ambiguous_and_quiz_resolves(env, tmp_path):
    from mdmdoc.consolidation import merge
    write_run(config.RUNS_DIR, "r", "bank", "ACCEPT", bank_run_fields())
    form = make_americas_form(tmp_path / "f.xlsm")  # form bank_key8 = 71000013
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    assert "BANKL" in m["ambiguous_fields"]
    srcs = {c["source"] for c in m["candidates"]["BANKL"]}
    assert srcs == {"doc_ach", "doc_wires", "form"}
    # default = document ACH
    assert m["rows"]["BUT0BK - Bank Account"][0]["BANKL"] == "072000326"
    # operator picks wires
    m2 = merge.merge_form_and_run(form, "r", tpl, source_id="N1",
                                  choices={"BANKL": "doc_wires"})
    assert m2["rows"]["BUT0BK - Bank Account"][0]["BANKL"] == "021000021"
    # operator keeps the form value
    m3 = merge.merge_form_and_run(form, "r", tpl, source_id="N1",
                                  choices={"BANKL": "form"})
    assert m3["rows"]["BUT0BK - Bank Account"][0]["BANKL"] == "71000013"
    tpl.close()


def test_document_fills_blank_and_wins(env, tmp_path):
    from mdmdoc.consolidation import merge
    write_run(config.RUNS_DIR, "r", "bank", "ACCEPT",
              bank_run_fields(holder="DOC HOLDER"))
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    bank = m["rows"]["BUT0BK - Bank Account"][0]
    assert bank["KOINH"] == "DOC HOLDER"        # form had none → document fills
    assert bank["BANKN"] == "000661570"          # account (form & doc agree)
    assert bank["BANKS"] == "US"
    tpl.close()


def test_single_routing_no_quiz_when_matches_form(env, tmp_path):
    from mdmdoc.consolidation import merge
    # document routing == form's (both 71000013): one candidate, no quiz
    write_run(config.RUNS_DIR, "r", "bank", "ACCEPT",
              bank_run_fields(routing_ach="71000013", routing_wires=None))
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    assert "BANKL" not in m["ambiguous_fields"]
    assert m["rows"]["BUT0BK - Bank Account"][0]["BANKL"] == "71000013"
    tpl.close()


def test_choices_are_source_keys_not_raw_values(env, tmp_path):
    from mdmdoc.consolidation import merge
    write_run(config.RUNS_DIR, "r", "bank", "ACCEPT", bank_run_fields())
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    # cross_check / candidates carry MASKED account, FULL routing
    disp = str(m["cross_check"]) + str(m["candidates"])
    assert "683661570" not in disp            # account never in display
    assert "072000326" in disp                # routing shown full
    # chosen is a source key, not a value
    assert m["chosen"]["BANKL"] in ("doc_ach", "doc_wires", "form")
    tpl.close()


def test_mismatch_warning_lists_written_and_form(env, tmp_path):
    from mdmdoc.consolidation import merge
    write_run(config.RUNS_DIR, "r", "bank", "ACCEPT", bank_run_fields())
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    assert any("written 072000326" in w and "form had 71000013" in w
               for w in m["warnings"])
    tpl.close()


def test_masked_bank_artifact_blocks(env, tmp_path):
    from mdmdoc.consolidation import merge
    f = bank_run_fields()
    f["account_number"] = {"present": True, "masked": "***570"}  # value stripped
    write_run(config.RUNS_DIR, "r", "bank", "ACCEPT", f)
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    assert m["errors"] and any("masked" in e for e in m["errors"])
    assert m["rows"] == {}
    tpl.close()


def test_national_clearing_becomes_bank_key(env, tmp_path):
    # a CN bank document carries the SAP Bank Key as a CNAPS clearing code
    # (national_clearing), NOT an ABA routing — it must still reach BANKL
    from mdmdoc.consolidation import merge
    write_run(config.RUNS_DIR, "cn", "bank", "WARNING", {
        "bank_country": "CN",
        "national_clearing": {"value": "303100000397", "present": True},
        "national_clearing_kind": "CNAPS",
        "account_number": {"value": "35310188000042676", "present": True,
                           "masked": "***676"},
        "account_holder": "上海市对外服务北京有限公司",
    })
    form = make_americas_form(tmp_path / "f.xlsm")  # form has an ABA-style key
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "cn", tpl, source_id="N1")
    srcs = {c["source"] for c in m["candidates"]["BANKL"]}
    assert "doc_clearing" in srcs
    # form ABA differs from the CNAPS -> ambiguous; picking the document writes it
    m2 = merge.merge_form_and_run(form, "cn", tpl, source_id="N1",
                                  choices={"BANKL": "doc_clearing"})
    assert m2["rows"]["BUT0BK - Bank Account"][0]["BANKL"] == "303100000397"
    # routing/bank key shown full (public)
    assert "303100000397" in str(m["candidates"]["BANKL"])
    tpl.close()


def test_w9_tax_and_name_from_document(env, tmp_path):
    from mdmdoc.consolidation import merge
    write_run(config.RUNS_DIR, "r", "w9", "ACCEPT", {
        "line1_name": "DOC LEGAL NAME", "tin_type": "SSN",
        "tin_raw": {"value": "000-11-2222", "present": True, "masked": "XXX-XX-2222"},
    })
    form = make_americas_form(tmp_path / "f.xlsm")  # form NAME1=SETH FAKESON, SSN 000-04-2016
    tpl = _tpl(tmp_path)
    m = merge.merge_form_and_run(form, "r", tpl, source_id="N1")
    lfa1 = m["rows"]["LFA1 - Supplier General"][0]
    # tax + name authoritative from the document (default)
    assert lfa1["STCD1"] == "000-11-2222"
    assert lfa1["NAME1"] == "DOC LEGAL NAME"
    # address / company-code data still from the form
    assert lfa1["ORT01"] == "GRAND RAPIDS"
    # TIN masked in cross_check
    assert "000-11-2222" not in str(m["cross_check"])
    tpl.close()
