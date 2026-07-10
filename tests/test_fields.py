import re
from pathlib import Path

from mdmdoc.fields import (Extraction, crosscheck_ids, norm_classification, to_iso2,
                           type_hint)


def test_crosscheck_fills_and_flags():
    fields = {"iban": "", "account_number": "999", "swift_bic": "DEUTDEFF"}
    det = {"iban": "DE44500105175407324931", "account_number": "1830042757",
           "swift_bic": "DEUTDEFF"}
    notes = crosscheck_ids(fields, det)
    assert fields["iban"] == "DE44500105175407324931"          # filled from OCR
    assert any(n.startswith("iban=filled-from-OCR") for n in notes)
    assert any(n.startswith("account_number=MISMATCH") for n in notes)
    assert "swift_bic=confirmed" in notes
    # notes must be masked
    assert "1830042757" not in " ".join(notes)
    assert "DE44500105175407324931" not in " ".join(notes)


def test_w9_tin_ocr_model_mismatch_flagged():
    """audit-wave C5: an OCR EIN that disagrees with the model TIN used to be
    silently dropped — no note, no signal. Now it is a hard-masked MISMATCH."""
    fields = {"tin_raw": "12-3456789", "tin_type": "SSN"}
    notes = crosscheck_ids(fields, {"ein": "98-7654321"}, doc_class="w9")
    assert any(n.startswith("tin_raw=MISMATCH") and "vs ocr=" in n for n in notes)
    assert fields["tin_raw"] == "12-3456789"          # model value not overwritten
    assert fields["tin_type"] == "SSN"                # contested read settles nothing
    blob = " ".join(notes)
    assert "123456789" not in blob and "12-3456789" not in blob   # hard-masked
    assert "987654321" not in blob and "98-7654321" not in blob


def test_w9_tin_ocr_confirm_sets_provenance():
    fields = {"tin_raw": "98-7654321"}
    prov = {"tin_raw": {"source": "model"}}
    notes = crosscheck_ids(fields, {"ein": "98-7654321"}, doc_class="w9", prov=prov)
    assert fields["tin_type"] == "EIN"
    assert prov["tin_raw"].get("confirmed") is True   # feeds the confidence gate
    assert not any("MISMATCH" in n for n in notes)


def test_w9_boxed_tin_mismatch_single_note():
    fields = {"tin_raw": "12-3456789"}
    det = {"ein": "98-7654321", "tin_boxed": "987654321", "tin_boxed_type": "EIN"}
    notes = crosscheck_ids(fields, det, doc_class="w9")
    assert sum(1 for n in notes if n.startswith("tin_raw=MISMATCH")) == 1
    assert fields.get("tin_type") in (None, "")       # nothing settled


def test_find_boxed_tin():
    from mdmdoc.fields import find_boxed_tin
    text = "Form W-9 boilerplate\nAmerican Epilepsy Society\n3\n6\n1\n2\n3\n4\n5\n6\n7\n06/09/2026\n"
    assert find_boxed_tin(text) == ("361234567", "")
    assert find_boxed_tin("only\n3\n6\ndigits\n") == ("", "")
    # dash line inside the run + preceding EIN label settles the type
    ein_text = ("Employer identification number\nNote: some instructions here\n"
                "8\n1\n–\n0\n8\n2\n6\n7\n3\n4\n Part II\n")
    assert find_boxed_tin(ein_text) == ("810826734", "EIN")
    ssn_text = "Social security number\n3\n2\n0\n5\n4\n0\n6\n9\n3\nor\n"
    assert find_boxed_tin(ssn_text) == ("320540693", "SSN")


def test_scrub_masks_boxed_tin_lines():
    from mdmdoc.privacy import SecretVault, scrub_text
    vault = SecretVault()
    vault.register("tin", "361234567")
    out = scrub_text("boxes:\n3\n6\n1\n2\n3\n4\n5\n6\n7\nend", vault)
    assert "3\n6\n1\n2\n3\n4\n5\n6\n7" not in out


def test_to_iso2():
    assert to_iso2("Germany") == "DE"
    assert to_iso2("de") == "DE"
    assert to_iso2("United States") == "US"
    assert to_iso2("") == ""


def test_norm_classification():
    assert norm_classification("Individual/sole proprietor or single-member LLC") == "individual_sole_prop"
    assert norm_classification("C Corporation") == "corporation"
    assert norm_classification("Limited liability company") == "llc"


def test_type_hint_invoice_and_w8():
    assert type_hint("invoice_123.pdf", "", ".pdf", "bank") == "invoice"
    assert type_hint("scan.pdf", "certificación bancaria of account", ".pdf", "bank") == "bank_letter"
    assert type_hint("doc.docx", "", ".docx", "bank") == "editable_source"
    assert type_hint("2026 05 w8ben.pdf", "", ".pdf", "w9") == "w8"


LETTER = ("Please accept this letter as confirmation that the account referenced below "
          "is maintained at Bank of America, N.A. ACH/EFT Routing Instructions ... "
          "Wire Routing Instructions ...")
INVOICE = ("Invoice\nInvoice Date ... Amount Due ... Payment Terms ... Subtotal ... "
           "Please note your invoice number and remit to the address below.")


def test_page_markers_detect_letter_and_invoice():
    from mdmdoc.fields import page_markers, page_score
    m = page_markers(LETTER)
    assert m["bank_letter"] and not m["invoice"] and not m["w9_form"]
    m = page_markers(INVOICE)
    assert m["invoice"] and not m["bank_letter"]
    # the confirmation letter must outrank the invoice template page
    assert page_score(LETTER, "bank") > page_score(INVOICE, "bank")


def test_invoice_text_hint_suppressed_by_bank_letter_page():
    # packet text containing BOTH an invoice footer and a bank confirmation letter
    packet_text = INVOICE + "\n" + LETTER
    assert type_hint("Customer Welcome Packet.pdf", packet_text, ".pdf", "bank") != "invoice"
    # pure invoice text still hints invoice
    assert type_hint("scan.pdf", INVOICE, ".pdf", "bank") == "invoice"


REMIT = ("(For ACH Payments Only - Do not use for wire transfers)\n"
         "Bank Name:\nIntrust Bank\nRouting Number:\n021000021\n"
         "Account Type:\nChecking\n"
         "Each ACH payment must be accompanied by an email remittance sent to\n"
         "billpay@example.com and must include the following:\n"
         "Customer Account Number\nInvoice Number(s) being paid\n"
         "Individual payment amount per invoice\n"
         "Example:\nAcct: 12345  |  Inv: 12345, 67890  |  Amt: $1,500.00, $500.00\n")


def test_remittance_instructions_are_not_invoice():
    from mdmdoc.fields import page_markers
    # "invoice" wording that only describes FUTURE invoices being paid is not
    # invoice evidence — this doc is supplier payment instructions (WARNING),
    # never REJECT-as-invoice
    assert page_markers(REMIT)["invoice"] is False
    assert type_hint("13-TBD ACH Payment Instructions.pdf", REMIT, ".pdf", "bank") \
        == "payment_instructions"
    # a real invoice (own number/date/amount) still classifies as invoice
    assert type_hint("scan.pdf", INVOICE, ".pdf", "bank") == "invoice"


def test_find_precedent(tmp_path, monkeypatch):
    import json
    from mdmdoc import config
    from mdmdoc.pipeline import _find_precedent
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "labels.jsonl")
    (tmp_path / "labels.jsonl").write_text(json.dumps(
        {"doc_sha256": "aa" * 8, "confirmed": True, "verdict_gold": "ACCEPT",
         "doc_type_gold": "bank_letter", "notes": "BOA letter p.3"}) + "\n")
    p = _find_precedent("aa" * 8)
    assert p and p["verdict_gold"] == "ACCEPT"
    assert _find_precedent("bb" * 8) is None


def test_to_public_masks_everything():
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"line1_name": "John Smith", "line2_business_name": "", "line3_classification":
                "Individual/sole proprietor", "tin_type": "SSN", "tin_raw": "320-54-0693",
                "address_street": "1 Main St", "address_city_state_zip": "", "signed": True,
                "sign_date": ""}
    e.register_secrets()
    pub = e.to_public()
    blob = str(pub)
    assert "320-54-0693" not in blob
    assert pub["fields"]["tin"]["masked"] == "XXX-XX-0693"
    assert pub["fields"]["tin"]["digits"] == 9
    assert pub["sensitive_present"] == {"tin": True}


def test_clean_path_handles_spaces_quotes_and_urls():
    from mdmdoc.cli import _clean_path
    assert _clean_path(["a", "file", "name.pdf"]) == "a file name.pdf"
    assert _clean_path(['"quoted path.pdf"']) == "quoted path.pdf"
    assert _clean_path(["escaped\\ space.pdf"]) == "escaped space.pdf"
    assert _clean_path(["file:///Users/x/a%20b.pdf"]) == "/Users/x/a b.pdf"
    assert _clean_path(["  trailing.pdf  "]) == "trailing.pdf"


def test_no_writes_outside_choke_points():
    """Source-level guard: only runstore/privacy/evalrun/dataset/fewshot/lora_export/
    modelfile/adoption may write files (they all call assert_no_leak or write
    non-document data)."""
    src = Path(__file__).resolve().parents[1] / "src" / "mdmdoc"
    # rules_io.py writes rules/*.yaml — no PII, so no leak gate, but a named write point
    # config.py writes settings.json (engine mode etc.) — operator state, no PII
    # synth.py writes eval/synthetic/ — PII-free by construction, leak-gated per row
    # rule_stats.py writes eval/rule_stats.json — rule ids + counts only, no PII
    allowed = {"runstore.py", "modelfile.py", "evalrun.py", "dataset.py",
               "fewshot.py", "lora_export.py", "adoption.py", "rules_io.py",
               "rule_approvals.py", "config.py", "synth.py", "rule_stats.py"}
    offenders = []
    for p in src.rglob("*.py"):
        if p.name in allowed:
            continue
        body = p.read_text()
        if re.search(r"write_text\(|open\([^)]*['\"]w", body):
            # cli.py --report writes the already-gated report; stage_a renders images via fitz
            if p.name == "cli.py" and body.count("write_text") == 1:
                continue
            offenders.append(p.name)
    assert not offenders, f"files writing outside choke points: {offenders}"
