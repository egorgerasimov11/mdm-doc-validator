"""Accuracy wave (2026-07-09): honest two-column scoring + rescore fidelity
(Batch 0), deterministic doc_type fixes (Batch 1) and the grounded
payment_instructions guard. All pure — no model calls."""
import json

from mdmdoc import config
from mdmdoc.evalrun import _field_match, _field_match_lenient
from mdmdoc.fields import (Extraction, _norm_name_lenient, names_materially_equal,
                           payment_instruction_marks, type_hint)
from mdmdoc.stage_a import RawDoc
from mdmdoc.stage_b import _ground_payment_instructions


# --- Batch 0: lenient name scoring -----------------------------------------
def test_lenient_normalizer_strips_legal_tokens():
    assert _norm_name_lenient("ConSol Consulting & Solutions Software GmbH") \
        == _norm_name_lenient("Consol Consulting Solutions Software")
    assert names_materially_equal("European Med Tech and IVD Reimbursement "
                                  "Consulting Ltd EOOD",
                                  "European Med Tech and IVD Reimbursement Consulting")
    assert names_materially_equal("John M. Zajecka, M.D.", "John M Zajecka")


def test_lenient_guardrails():
    assert not names_materially_equal("", "Acme GmbH")          # empty never matches
    assert not names_materially_equal("SA", "Banco Real SA")    # bare legal token
    assert not names_materially_equal("Acme Industries", "Zenith Corp")
    # subset needs a shared token of length >= 3
    assert not names_materially_equal("AB CD", "AB CD EF")


def test_field_match_lenient_only_for_name_fields():
    pred = {"account_holder": "Snaco", "iban": {"masked": "DE**1", "present": True}}
    gold = {"account_holder": "Snaco GmbH", "iban": {"masked": "DE**2", "present": True}}
    assert not _field_match("account_holder", pred, gold)       # strict miss
    assert _field_match_lenient("account_holder", pred, gold)   # lenient hit
    assert not _field_match_lenient("iban", pred, gold)         # IDs never lenient


def test_rescore_fidelity_and_anchor(monkeypatch, tmp_path):
    from mdmdoc import evalrun, runstore
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "eval").mkdir()
    rid = "a" * 16
    (tmp_path / "runs" / rid).mkdir(parents=True)
    (tmp_path / "runs" / rid / "extraction.json").write_text(json.dumps(
        {"fields": {"account_holder": "Snaco", "bank_name": "Commerzbank",
                    "iban": {"masked": "DE**…6600", "present": True},
                    "swift_bic": "COBADEFFXXX",
                    "account_number": {"masked": "…6600", "present": True}}}))
    label = {"doc_sha256": rid, "doc_class": "bank",
             "fields_gold": {"account_holder": "Snaco GmbH",
                             "bank_name": "Commerzbank",
                             "iban": {"masked": "DE**…6600", "present": True},
                             "swift_bic": "COBADEFFXXX",
                             "account_number": {"masked": "…6600", "present": True}}}
    monkeypatch.setattr(evalrun, "load_labels", lambda: [label])
    # recorded strict row: holder strict-miss, everything else hit
    (tmp_path / "eval" / "last_results.json").write_text(json.dumps(
        {"tag": "fixture", "rows": [{"file": "snaco.pdf", "run_id": rid,
                                     "fields": {"account_holder": False,
                                                "bank_name": True, "iban": True,
                                                "swift_bic": True,
                                                "account_number": True}}]}))
    assert evalrun.run_rescore(tag="anchor-test", record=True) == 0  # fidelity OK
    hist = (tmp_path / "eval" / "history.jsonl").read_text().strip().splitlines()
    entry = json.loads(hist[-1])
    assert entry["rescore"] is True
    assert entry["metrics"]["fields"]["bank.account_holder"] == 0.0      # strict
    assert entry["metrics"]["fields_lenient"]["bank.account_holder"] == 1.0


# --- Batch 1: deterministic doc_type ---------------------------------------
def test_supplier_letterhead_beats_ap_document():
    text = ("Dear Vendor/Customer, please find legal, contact, and bank account "
            "information for contracting. We use DocuSign for signatures.")
    assert type_hint("company info.pdf", text, ".pdf", "bank") == "supplier_letterhead"


def test_ap_document_needs_sap_marker():
    text = "Bank Account Information form for SAP registration for HCP"
    assert type_hint("takaki.pdf", text, ".pdf", "bank") == "ap_document"
    # docusign alone is no longer AP evidence
    text2 = "Bank account information sheet. Signed via DocuSign."
    assert type_hint("x.pdf", text2, ".pdf", "bank") != "ap_document"


def test_settlement_instructions_are_payment_instructions():
    text = ("Standard Settlement Instructions for USD Account. "
            "For Wire Transfers: ... For ACH Delivery: ...")
    assert type_hint("jpm.pdf", text, ".pdf", "bank") == "payment_instructions"
    assert payment_instruction_marks(text)


def test_cjk_invoice_labels_not_bare_word():
    assert payment_instruction_marks("请提供 standard settlement instructions")
    from mdmdoc.fields import invoice_marks
    # a solicitation merely PROMISING an invoice is not invoice evidence
    assert invoice_marks("上传合同文本及付款凭证，申请开具发票") == 0
    # real CJK invoice field labels are
    assert invoice_marks("发票号码: 123\n发票日期: 2026-01-01") >= 2


def test_ground_payment_instructions_guard():
    raw = RawDoc(path="/x/n.pdf", sha256="e" * 64, ext=".pdf", doc_class="bank")
    raw.raw_text = "湖南省医师协会 展览通知 汇款账户 账号 户名"   # no deterministic markers
    raw.type_hint = ""
    ext = Extraction(doc_class="bank", doc_type="payment_instructions")
    _ground_payment_instructions(ext, raw)
    assert ext.doc_type == "other"
    assert any("no payment-instruction markers" in w for w in ext.warnings)
    # grounded stays
    raw2 = RawDoc(path="/x/j.pdf", sha256="f" * 64, ext=".pdf", doc_class="bank")
    raw2.raw_text = "Standard Settlement Instructions for USD Account"
    ext2 = Extraction(doc_class="bank", doc_type="payment_instructions")
    _ground_payment_instructions(ext2, raw2)
    assert ext2.doc_type == "payment_instructions"
