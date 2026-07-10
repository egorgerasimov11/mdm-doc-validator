"""S1 (quality wave): the deterministic signature layer. One fold point
(_resolve_signature) across the text esign channel, the vision probe and the
typed-officer compensating evidence; a NEGATIVE probe never overwrites
signature_evidence (the real Citizens regression), and the e-signature guard
covers W-9/W-8 too (the real Motion regression)."""
from types import SimpleNamespace

import pytest

from mdmdoc import stage_b
from mdmdoc.fields import Extraction, detect_officer_block
from mdmdoc.rules.engine import run_rules
from mdmdoc.verdict import decide

CITIZENS_SHAPE = """Vela Federal Bank
EXAMPLE VENDOR LLC
Re: Account Confirm
Bank Account Number: 000111222333
We hereby confirm that Example Vendor LLC is known to us and has accounts in
good standing at Vela Federal Bank.
Sincerely,

Jordan Q. Sample J.D.
Vice President | Relationship Manager
Vela Federal Bank Business Banking
Mobile: 000-000-0000 | Fax: 000-000-0000
jordan.sample@velafederal.example
"""

W9_ESIG_SHAPE = """Form W-9 Request for Taxpayer Identification Number
1 Name: Example Vendor LLC
Sign Here  Signature of U.S. person   Alex T. Sample
Digitally signed by Alex T. Sample
Date: 2026.01.27 11:51:15 -06'00'
"""


def _raw(text, doc_class="bank", probe=None, w9_pages=(), pages_used=(0,)):
    return SimpleNamespace(raw_text=text, signature_probe=probe or {},
                           w9_pages=list(w9_pages), pages_used=list(pages_used))


def _bank_ext(**fields):
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"signed": False, "signature_evidence": "", **fields}
    return e


def _w9_ext(**fields):
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"signed": False, "sign_date": "", **fields}
    return e


# --- officer-block detector ---------------------------------------------------
def test_officer_block_detector_fires_on_signoff_name_title_contact():
    fired, snippet = detect_officer_block(CITIZENS_SHAPE)
    assert fired
    assert "Jordan Q. Sample" in snippet and "Vice President" in snippet


def test_officer_block_not_fired_on_inline_signoff():
    # inline closing (the synthetic corpus shape) must NOT qualify
    fired, _ = detect_officer_block(
        "We confirm the account.\nSincerely, Vela Federal Bank Account Services.\n")
    assert not fired


def test_officer_block_needs_all_three_lines():
    no_title = CITIZENS_SHAPE.replace("Vice President | Relationship Manager",
                                      "for any questions call us")
    assert not detect_officer_block(no_title)[0]
    no_name = CITIZENS_SHAPE.replace("Jordan Q. Sample J.D.", "the banking team")
    assert not detect_officer_block(no_name)[0]


# --- e-signature guard for w9 -------------------------------------------------
def test_esign_guard_fires_on_digitally_signed_w9():
    ext = _w9_ext()
    stage_b._esignature_guard(ext, _raw(W9_ESIG_SHAPE, "w9"))
    assert ext.fields["signed"] is True
    assert ext.fields["signature_kind"] == "electronic"
    assert ext.fields["sign_date"] == "2026.01.27"
    f = run_rules(ext, enforce_approvals=False)
    assert not any(x.rule_id == "W9-020" for x in f)   # Motion regression


def test_w9_truly_unsigned_still_fires_w9020():
    ext = _w9_ext(line1_name="Example Vendor LLC",
                  line3_classification="C corporation",
                  tin_type="EIN", tin_raw="00-1234567")
    stage_b._esignature_guard(ext, _raw("Form W-9 no marks here", "w9"))
    stage_b._resolve_signature(ext, _raw("Form W-9 no marks here", "w9"))
    assert ext.fields["signed"] is False
    f = run_rules(ext, enforce_approvals=False)
    assert any(x.rule_id == "W9-020" for x in f)


# --- the resolve fold ----------------------------------------------------------
NEG_PROBE = {"handwritten_signature": False, "stamp": False,
             "evidence": "No handwritten signature or ink stamp/seal present.",
             "page": 0, "votes": {"band": "neg", "page": "neg", "text": "none"},
             "uncertain": False}


def test_negative_probe_never_overwrites_officer_evidence():
    """The Citizens regression: text tier carried officer evidence, the
    negative probe used to erase it -> BNK-021 instead of BNK-026."""
    ext = _bank_ext(account_holder="Example Vendor LLC",
                    bank_name="Vela Federal Bank", account_number="000111222333",
                    signed=True,
                    signature_evidence="typed officer block: Jordan Q. Sample")
    raw = _raw(CITIZENS_SHAPE, probe=NEG_PROBE)
    stage_b._officer_block_guard(ext, raw)
    stage_b._resolve_signature(ext, raw)
    assert ext.fields["signed"] is False               # vision is right: no wet ink
    assert ext.fields["signature_kind"] == "none"
    assert "officer block" in ext.fields["signature_evidence"]   # PRESERVED
    f = run_rules(ext, enforce_approvals=False)
    assert any(x.rule_id == "BNK-026" for x in f)
    assert not any(x.rule_id == "BNK-021" for x in f)
    assert decide(f) == "ACCEPT"


def test_officer_fill_after_resolve_even_when_model_said_signed():
    """Model evidence may be non-positive garbage; the officer DETECTOR plus
    the post-resolve fill must still land BNK-026-grade evidence."""
    ext = _bank_ext(account_holder="Example Vendor LLC",
                    bank_name="Vela Federal Bank", account_number="000111222333",
                    signed=True, signature_evidence="signature block at bottom")
    raw = _raw(CITIZENS_SHAPE, probe=NEG_PROBE)
    stage_b._officer_block_guard(ext, raw)
    stage_b._resolve_signature(ext, raw)
    assert ext.fields["officer_block"] is True
    assert ext.fields["signature_evidence"].startswith("typed officer block")
    f = run_rules(ext, enforce_approvals=False)
    assert any(x.rule_id == "BNK-026" for x in f)
    assert not any(x.rule_id == "BNK-021" for x in f)


def test_bare_unsigned_letter_still_fires_bnk021():
    ext = _bank_ext(account_holder="Vendor", bank_name="Bank",
                    account_number="000111222333")
    raw = _raw("We confirm the account details above.\n", probe=NEG_PROBE)
    stage_b._officer_block_guard(ext, raw)
    stage_b._resolve_signature(ext, raw)
    f = run_rules(ext, enforce_approvals=False)
    assert any(x.rule_id == "BNK-021" for x in f)
    assert decide(f) == "WARNING"


def test_typed_system_fill_bnk026():
    ext = _bank_ext(account_holder="Vendor", bank_name="Bank",
                    account_number="000111222333")
    text = "This is a computer generated confirmation and requires no signature."
    raw = _raw(text, probe=NEG_PROBE)
    stage_b._officer_block_guard(ext, raw)
    stage_b._resolve_signature(ext, raw)
    assert "computer-generated notice" in ext.fields["signature_evidence"]
    f = run_rules(ext, enforce_approvals=False)
    assert any(x.rule_id == "BNK-026" for x in f)
    assert not any(x.rule_id == "BNK-021" for x in f)


def test_positive_probe_sets_wet_kind():
    probe = {"handwritten_signature": True, "stamp": False,
             "evidence": "handwritten signature above the printed name",
             "page": 0, "votes": {"band": "pos", "page": "not-run", "text": "none"},
             "uncertain": False}
    ext = _bank_ext()
    stage_b._resolve_signature(ext, _raw("letter", probe=probe))
    assert ext.fields["signed"] is True
    assert ext.fields["signature_kind"] == "wet"


def test_stamp_only_bank_vs_w9():
    probe = {"handwritten_signature": False, "stamp": True,
             "evidence": "red circular stamp", "page": 0,
             "votes": {"band": "pos", "page": "not-run", "text": "none"},
             "uncertain": False}
    ext = _bank_ext()
    stage_b._resolve_signature(ext, _raw("letter", probe=dict(probe)))
    assert ext.fields["signed"] is True and ext.fields["signature_kind"] == "stamp"
    w9 = _w9_ext()
    stage_b._resolve_signature(w9, _raw("form", "w9", probe=dict(probe)))
    assert w9.fields["signed"] is False
    assert any("stamp is not a signature" in w for w in w9.warnings)


def test_scoped_out_w9_page_keeps_text_signed():
    """A bank packet whose probe landed on the W-9 page: the tax form's
    signature must not sign the banking sheet (Zajecka backstop)."""
    probe = {"handwritten_signature": True, "stamp": False,
             "evidence": "signature", "page": 1,
             "votes": {"band": "pos", "page": "not-run", "text": "none"},
             "uncertain": False}
    ext = _bank_ext()
    raw = _raw("packet", probe=probe, w9_pages=[1], pages_used=[0, 1])
    stage_b._resolve_signature(ext, raw)
    assert ext.fields["signed"] is False               # text tier said False
    assert ext.signature_probe.get("scoped_out_w9_page") is True
    assert ext.signature_probe.get("uncertain") is True
    assert any("W-9 section" in w for w in ext.warnings)


def test_esign_short_circuit_in_resolve():
    ext = _bank_ext()
    raw = _raw("DocuSign Envelope ID: TEST-1. Electronically signed.",
               probe=NEG_PROBE)                       # even a negative probe
    stage_b._resolve_signature(ext, raw)
    assert ext.fields["signed"] is True                # esign channel wins
    assert ext.fields["signature_kind"] == "electronic"
