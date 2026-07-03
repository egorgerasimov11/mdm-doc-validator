"""Tests for the quality overhaul: display/gate policy split, multi-ABA and
spaced-EIN extraction, date-is-not-TIN guard, tier escalation and merge."""
import pytest

from mdmdoc.fields import Extraction
from mdmdoc.ocr import regex_fields
from mdmdoc.privacy import (SecretVault, assert_no_leak, display_value, mask,
                            scrub_text)

CITIZENS_TEXT = """EUREKACONNECT LLC
Re: Account Confirm
Bank Account Name:
CHECKING ACCOUNT
Bank Account Number:
1408137817
IBAN Number:
N/A
Bank Name:
Citizens Bank
Bank ABA (standard):
211070175
Bank ABA (domestic wires):
011500120
Bank SWIFT:
CTZIUS33
"""


# ---------------------------------------------------------------- policy ------
def test_display_value_tin_always_masked():
    assert display_value("tin", "81-0826734", "full") == "XX-XXX6734"
    assert display_value("ssn", "320-54-0693", "full") == "XXX-XX-0693"
    assert display_value("iban", "DE44500105175407324931", "full") == "DE44500105175407324931"
    assert display_value("iban", "DE44500105175407324931", "masked") == "DE**…4931"
    assert display_value("routing_aba", "211070175", "full") == "211070175"


def test_tin_only_gate_allows_banking_blocks_tin():
    blob = "account 1408137817 iban DE44500105175407324931 routing 211070175"
    assert assert_no_leak(blob, policy="tin-only") == []          # banking OK
    with pytest.raises(ValueError):
        assert_no_leak(blob + " ssn 320-54-0693", policy="tin-only")
    with pytest.raises(ValueError):
        assert_no_leak("ein 81-0826734", policy="tin-only")
    with pytest.raises(ValueError):                                # spaced boxes
        assert_no_leak("boxes 8 1 - 0 8 2 6 7 3 4 end", policy="tin-only")
    with pytest.raises(ValueError):                                # known raw TIN
        assert_no_leak("value 810826734 here", ["81-0826734"], policy="tin-only")
    with pytest.raises(ValueError):                                # strict blocks banking
        assert_no_leak(blob, policy="strict")


def test_scrub_tin_only_keeps_banking_masks_tin():
    vault = SecretVault()
    vault.register("tin", "81-0826734")
    vault.register("account_number", "1408137817")
    out = scrub_text("acct 1408137817 ein 81-0826734", vault, policy="tin-only")
    assert "1408137817" in out and "81-0826734" not in out
    strict = scrub_text("acct 1408137817 ein 81-0826734", vault, policy="strict")
    assert "1408137817" not in strict and "81-0826734" not in strict


def test_to_public_policy_full_banking_value_tin_never():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"account_number": "1408137817", "routing_aba": "211070175",
                "routing_aba_wires": "011500120", "iban": ""}
    pub = e.to_public(policy="full")
    assert pub["fields"]["account_number"]["value"] == "1408137817"
    assert pub["fields"]["routing_aba_wires"]["value"] == "011500120"
    w = Extraction(doc_class="w9", doc_type="w9")
    w.fields = {"tin_type": "EIN", "tin_raw": "81-0826734"}
    tin = w.to_public(policy="full")["fields"]["tin"]
    assert "value" not in tin and tin["masked"] == "XX-XXX6734"


# ---------------------------------------------------------------- regex -------
def test_multi_aba_extraction():
    out = regex_fields(CITIZENS_TEXT)
    assert out["routing_aba"] == "211070175"
    assert out["routing_aba_wires"] == "011500120"
    assert out["account_number"] == "1408137817"
    assert out["swift_bic"] == "CTZIUS33"


def test_spaced_ein_near_label():
    text = ("Employer identification number\n8 1 – 0 8 2 6 7 3 4\n")
    out = regex_fields(text)
    assert out.get("ein") == "81-0826734"


# ---------------------------------------------------------------- stage B -----
def test_date_is_never_tin():
    from mdmdoc.stage_b import _normalize_tin
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"tin_type": "EIN", "tin_raw": "1/1/2026", "sign_date": ""}
    _normalize_tin(e)
    assert e.fields["tin_raw"] == ""
    assert e.fields["sign_date"] == "1/1/2026"


def test_escalation_reasons_table():
    from mdmdoc.stage_a import RawDoc
    from mdmdoc.stage_b import escalation_reasons
    raw = RawDoc(path="/x/d.pdf", sha256="a" * 64, ext=".pdf", doc_class="bank")
    raw.raw_text = "Bank ABA routing details for the account"
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"bank_country": "USA", "routing_aba": "", "account_number": "123456",
                "iban": ""}
    r = escalation_reasons(e, raw, quality=False)
    assert "us-bank-no-routing" in r
    e.fields["routing_aba"] = "211070175"
    assert "us-bank-no-routing" not in escalation_reasons(e, raw, False)
    assert "quality-requested" in escalation_reasons(e, raw, True)
    w = Extraction(doc_class="w9", doc_type="w9")
    w.fields = {"tin_raw": "", "line3_classification": "LLC"}
    wraw = RawDoc(path="/x/w.pdf", sha256="b" * 64, ext=".pdf", doc_class="w9")
    wraw.raw_text = "w9 text"
    assert "w9-no-tin" in escalation_reasons(w, wraw, False)
    e2 = Extraction(doc_class="bank", doc_type="invoice")   # hard-reject: no escalation
    e2.fields = {"bank_country": "US", "routing_aba": ""}
    assert "us-bank-no-routing" not in escalation_reasons(e2, raw, False)


def test_merge_tiers_rules():
    from mdmdoc.stage_b import _merge_tiers
    fast = {"a": "", "b": "keep", "c": "old", "signed": False}
    strong = {"a": "filled", "b": "", "c": "new", "signed": True}
    merged, notes = _merge_tiers(fast, strong, ["a", "b", "c", "signed"])
    assert merged["a"] == "filled"        # strong fills the gap
    assert merged["b"] == "keep"          # strong never blanks a value
    assert merged["c"] == "new"           # strong wins disagreements…
    assert any("tier disagreement: c" in n for n in notes)   # …with a note
    assert merged["signed"] is True and any("signed" in n for n in notes)


def test_w9_zone_probe_settles_checkbox_and_tin():
    """Denver case: text guess 'Individual' while S corporation is checked;
    boxed EIN skipped — the zone-crop evidence must win."""
    from mdmdoc.stage_a import RawDoc
    from mdmdoc.stage_b import _apply_w9_zone_probe
    raw = RawDoc(path="/x/w9.pdf", sha256="c" * 64, ext=".pdf", doc_class="w9")
    raw.w9_probe = {"classification": "S corporation", "llc_code": "",
                    "tin_type": "EIN", "tin_digits": "481159914",
                    "class_evidence": "checkmark in S corporation box"}
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"line3_classification": "Individual/sole proprietor",
                "tin_raw": "", "tin_type": ""}
    _apply_w9_zone_probe(e, raw)
    assert e.fields["line3_classification"] == "S corporation"
    assert e.fields["tin_raw"] == "48-1159914" and e.fields["tin_type"] == "EIN"
    assert any("visual checkbox" in w for w in e.warnings)
    assert "481159914" not in " ".join(e.warnings)      # warnings stay masked


def test_line_swap_not_fired_for_dba():
    """Line 1 = legal Inc., Line 2 = 'Culligan of Denver' (DBA) is NORMAL."""
    from mdmdoc.rules.predicates import line_swap_suspect
    flds = {"line1_name": "Wichita Water Conditioning, Inc.",
            "line2_business_name": "Culligan of Denver",
            "line3_classification": "Individual/sole proprietor"}  # even mis-read
    fired, _ = line_swap_suspect("", flds, {}, {})
    assert fired is False
    # real person on line 2 of an Individual with business line 1 still fires
    flds2 = {"line1_name": "ACME LLC", "line2_business_name": "John Smith",
             "line3_classification": "Individual/sole proprietor"}
    assert line_swap_suspect("", flds2, {}, {})[0] is True
    # empty line 1 still fires
    assert line_swap_suspect("", {"line1_name": "", "line2_business_name": "X",
                                  "line3_classification": ""}, {}, {})[0] is True


def test_filename_echo_dropped():
    """'Dr. Clarke' from '...Donation from Dr. Clarke.pdf' is not a Line 1."""
    from mdmdoc.stage_a import RawDoc
    from mdmdoc.stage_b import _drop_filename_echo
    raw = RawDoc(path="/x/AES W9 Donation from Dr. Clarke.pdf", sha256="d" * 64,
                 ext=".pdf", doc_class="w9")
    raw.raw_text = "Form W-9 ... American Epilepsy Society ... Chicago IL"
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"line1_name": "Dr. Clarke", "line2_business_name": ""}
    _drop_filename_echo(e, raw)
    assert e.fields["line1_name"] == ""
    assert any("filename echo" in w for w in e.warnings)
    # a name present in BOTH filename and document is legitimate — kept
    e2 = Extraction(doc_class="w9", doc_type="w9")
    e2.fields = {"line1_name": "American Epilepsy Society"}
    raw2 = RawDoc(path="/x/American Epilepsy Society W9.pdf", sha256="e" * 64,
                  ext=".pdf", doc_class="w9")
    raw2.raw_text = "American Epilepsy Society, Chicago"
    _drop_filename_echo(e2, raw2)
    assert e2.fields["line1_name"] == "American Epilepsy Society"


def test_ein_detector_settles_tin_type():
    from mdmdoc.fields import crosscheck_ids
    fields = {"tin_raw": "", "tin_type": "SSN"}     # model guessed SSN
    notes = crosscheck_ids(fields, {"ein": "81-0826734"}, doc_class="w9")
    assert fields["tin_raw"] == "81-0826734"
    assert fields["tin_type"] == "EIN"
    assert any("settled" in n for n in notes)


# ---------------------------------------------------------------- labels ------
def test_label_path_remasks_full_policy_artifacts(tmp_path, monkeypatch):
    """Run artifacts under the full display policy contain full banking values;
    the label built from them must be fully masked (strict, always)."""
    import json

    from mdmdoc import config, review_core
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    rid = "feed0123feed0123"
    d = tmp_path / "runs" / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"path": "/x/doc.pdf", "file_name": "doc.pdf", "doc_class": "bank"}))
    (d / "extraction.json").write_text(json.dumps({
        "doc_class": "bank", "doc_type": "bank_letter",
        "fields": {"account_holder": "EUREKACONNECT LLC", "account_type": "CHECKING ACCOUNT",
                   "bank_name": "Citizens Bank", "bank_country": "USA", "bank_address": "",
                   "iban": {"present": False, "masked": ""},
                   "swift_bic": "CTZIUS33",
                   "account_number": {"present": True, "masked": "…7817",
                                      "length": 10, "value": "1408137817"},
                   "routing_aba": {"present": True, "masked": "…0175",
                                   "length": 9, "value": "211070175"},
                   "routing_aba_wires": {"present": True, "masked": "…0120",
                                         "length": 9, "value": "011500120"},
                   "currency": "", "doc_date": "", "signed": False,
                   "signature_evidence": "typed officer block", "partial_capture": False}}))
    (d / "stage_a.json").write_text(json.dumps({
        "raw_text_excerpt": "Account Number: 1408137817 ABA 211070175 wires 011500120",
        "regex_candidates_masked": {"account_number": "1408137817",
                                    "routing_aba": "211070175",
                                    "routing_aba_wires": "011500120",
                                    "swift_bic": "CTZIUS33"}}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))
    review_core.submit_review(rid, {"fields": {}, "notes": "keep-all"})
    blob = config.LABELS_PATH.read_text()
    for full in ("1408137817", "211070175", "011500120"):
        assert full not in blob, f"label leaked {full}"
    assert "…7817" in blob
    lab = json.loads(blob.strip().splitlines()[-1])
    assert "value" not in json.dumps(lab["fields_gold"])
