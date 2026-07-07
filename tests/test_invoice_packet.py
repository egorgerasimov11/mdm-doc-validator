"""A genuine invoice page inside a packet (with NO bank confirmation letter) must
classify as `invoice` → REJECT (BNK-001), not `payment_instructions` → WARNING.
Real case: Jamcorder-INV-2004 — page 1 is a real invoice (own invoice no/date/
amount), pages 2-3 are Mercury/Column payment instructions; the deep-read used
the payment pages, so the model saw only payment_instructions."""
from mdmdoc.fields import BANK_KEYS, Extraction
from mdmdoc.rules.engine import run_rules
from mdmdoc.verdict import decide


def test_invoice_doc_type_rejects_via_bnk001():
    ext = Extraction(doc_class="bank", doc_type="invoice")
    ext.fields = {k: "" for k in BANK_KEYS}
    ext.fields.update({"account_holder": "Jamcorder LLC", "bank_name": "Column N.A."})
    f = run_rules(ext)
    assert decide(f) == "REJECT"
    assert any(x.rule_id == "BNK-001" for x in f)
    assert not any(x.rule_id == "BNK-004" for x in f)   # payment_instructions rule must not apply


def test_invoice_page_in_packet_classifies_invoice(monkeypatch):
    from mdmdoc import model_client as mc, stage_b
    from mdmdoc.stage_a import RawDoc
    raw = RawDoc(path="/x/Jamcorder-INV-2004.pdf", sha256="f" * 64, ext=".pdf", doc_class="bank")
    raw.raw_text = ("Payment Instructions\nBeneficiary name Jamcorder LLC\n"
                    "Account number 591564501132927\nABA routing number 121145433")
    raw.invoice_pages = [0]          # survey flagged page 1 as a genuine invoice
    raw.bank_letter_pages = []       # no bank letter to rescue it
    raw.pages_used = [2, 1]          # deep-read only the payment-instructions pages
    raw.type_hint = ""               # so the type_hint override does NOT fire
    # the model saw only the payment pages -> payment_instructions
    monkeypatch.setattr(stage_b, "_run_model",
                        lambda raw, role: ({"doc_type": "payment_instructions",
                                            "fields": {k: "" for k in BANK_KEYS}}, True, "stub"))
    monkeypatch.setattr(mc, "strong_distinct", lambda: False)   # no escalation
    monkeypatch.setattr(mc, "resolve", lambda role: "stub")
    monkeypatch.setattr(mc, "unload", lambda *a, **k: None)

    ext = stage_b.extract(raw)
    assert ext.doc_type == "invoice"                      # invoice page wins -> REJECT territory
    assert any("genuine invoice page" in w for w in ext.warnings)
    # and it verdicts REJECT through the rule engine
    assert decide(run_rules(ext)) == "REJECT"
