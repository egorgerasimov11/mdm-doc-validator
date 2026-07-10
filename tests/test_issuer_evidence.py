"""G3: issuer-aware evidence for payment_instructions — bank-issued standard
settlement instructions (JPM case) vs a supplier's self-made ACH sheet."""
from mdmdoc import doctype_evidence, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

SSI = ("JPMorgan Chase Bank N.A.\nStandard Settlement Instructions\n"
       "Account held with JPMorgan Chase Bank N.A.\n"
       "Sincerely,\nJordan Q. Sample\nVice President\nTel: +1 212 000 0000\n")
SUPPLIER = "Please update our ACH remittance details as follows. Thanks, Vendor Inc."


def _run(text, doc_type="payment_instructions", officer=True):
    ext = Extraction(doc_class="bank", doc_type=doc_type)
    ext.fields = {"officer_block": True} if officer else {}
    raw = RawDoc(path="x.pdf", sha256="9" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = text
    stage_b._record_settlement_issuer(ext, raw)
    return ext


def test_bank_issued_ssi_flags_strong():
    ext = _run(SSI)
    comp = ext.doc_subtype_evidence["settlement_issuer"]
    assert comp["bank_identity"] and comp["account_held"] and comp["issuer_strong"]
    assert ext.fields["settlement_issuer_strong"] is True


def test_supplier_sheet_not_strong():
    ext = _run(SUPPLIER, officer=False)
    comp = ext.doc_subtype_evidence["settlement_issuer"]
    assert not comp["issuer_strong"]
    assert "settlement_issuer_strong" not in ext.fields


def test_other_doc_types_ignored():
    ext = _run(SSI, doc_type="bank_letter")
    assert not ext.doc_subtype_evidence


def test_bnk027_fires_on_flag_unenforced():
    from mdmdoc.rules.engine import run_rules
    ext = _run(SSI)
    ext.fields.update({"account_holder": "Fake Corp", "bank_name": "JPMorgan",
                       "signed": True, "account_number": "12345678"})
    findings = run_rules(ext, enforce_approvals=False)
    f = next(x for x in findings if x.rule_id == "BNK-027")
    assert f.severity == "NOTE" and f.verdict_effect is None
