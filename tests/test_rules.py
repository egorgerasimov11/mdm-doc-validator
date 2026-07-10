from mdmdoc.fields import Extraction
from mdmdoc.rules.engine import run_rules
from mdmdoc.verdict import decide


def _bank(doc_type, **fields):
    e = Extraction(doc_class="bank", doc_type=doc_type)
    e.fields = {"account_holder": "", "bank_name": "", "bank_country": "", "bank_address": "",
                "iban": "", "swift_bic": "", "account_number": "", "routing_aba": "",
                "currency": "", "doc_date": "", "signed": True, "partial_capture": False}
    e.fields.update(fields)
    return e


def _w9(doc_type, **fields):
    e = Extraction(doc_class="w9", doc_type=doc_type)
    e.fields = {"line1_name": "", "line2_business_name": "", "line3_classification": "",
                "tin_type": "", "tin_raw": "", "address_street": "",
                "address_city_state_zip": "", "signed": True, "sign_date": ""}
    e.fields.update(fields)
    return e


def test_invoice_always_rejects():
    f = run_rules(_bank("invoice", account_holder="ACME", iban="DE44500105175407324931"))
    assert decide(f) == "REJECT"
    assert any(x.rule_id == "BNK-001" for x in f)


def test_email_and_editable_reject():
    assert decide(run_rules(_bank("email"))) == "REJECT"
    assert decide(run_rules(_bank("editable_source"))) == "REJECT"


def test_good_bank_letter_accepts():
    f = run_rules(_bank("bank_letter", account_holder="ACME GmbH", bank_name="Deutsche Bank",
                        bank_country="DE", iban="DE44500105175407324931",
                        swift_bic="DEUTDEFF"))
    assert decide(f) == "ACCEPT"


def test_iban_wrong_length_flags():
    f = run_rules(_bank("bank_letter", account_holder="ACME", bank_name="DB",
                        bank_country="DE", iban="DE4450010517540732493"))  # 21, DE needs 22
    assert decide(f) == "NEED_MANUAL_REVIEW"
    assert any(x.rule_id == "BNK-011" for x in f)


def test_swift_country_mismatch_flags():
    f = run_rules(_bank("bank_letter", account_holder="ACME", bank_name="DB",
                        bank_country="DE", iban="DE44500105175407324931",
                        swift_bic="DEUTDGBF"))  # chars 5-6 = GB, bank DE
    assert any(x.rule_id == "BNK-010" for x in f)


def test_unsigned_bank_letter_warns():
    f = run_rules(_bank("bank_letter", account_holder="ACME", bank_name="DB",
                        account_number="12345678", signed=False))
    assert decide(f) == "WARNING"
    assert any(x.rule_id == "BNK-021" for x in f)


def test_negative_signature_statement_is_not_evidence():
    # "no signature is present" states ABSENCE — it must not silence BNK-021
    f = run_rules(_bank("bank_letter", account_holder="ACME", bank_name="DB",
                        account_number="12345678", signed=False,
                        signature_evidence="No handwritten signature or ink stamp/seal is present."))
    assert decide(f) == "WARNING"
    assert any(x.rule_id == "BNK-021" for x in f)


def test_computer_generated_statement_is_evidence():
    # a bank-standard computer-generated notice compensates: NOTE, not WARNING
    f = run_rules(_bank("bank_letter", account_holder="ACME", bank_name="DB",
                        account_number="12345678", signed=False,
                        signature_evidence="This is a computer generated confirmation "
                                           "and requires no signature."))
    assert decide(f) == "ACCEPT"
    assert any(x.rule_id == "BNK-026" for x in f)
    assert not any(x.rule_id == "BNK-021" for x in f)


def test_bank_statement_no_unsigned_noise_and_swift_gap_note():
    # statements carry no signature by design: no BNK-021/026; missing SWIFT is a
    # NOTE pointing at the SAP/form comparison, and the verdict stays ACCEPT
    f = run_rules(_bank("bank_statement", account_holder="IQH LABS PTE. LTD.",
                        bank_name="DBS Bank Ltd", account_number="072-154506-3",
                        signed=False))
    assert decide(f) == "ACCEPT"
    assert any(x.rule_id == "BNK-006" for x in f)
    assert not any(x.rule_id in ("BNK-021", "BNK-026") for x in f)


def test_bank_statement_missing_holder_is_nmr():
    """audit-wave C7: bank_statement was excluded from BNK-023/024/025, so a
    holder-less statement could ACCEPT (worst in degraded no-LLM mode)."""
    f = run_rules(_bank("bank_statement", bank_name="DBS Bank Ltd",
                        account_number="072-154506-3", signed=False))
    assert any(x.rule_id == "BNK-023" for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_bank_statement_no_ids_is_nmr():
    f = run_rules(_bank("bank_statement", account_holder="IQH LABS PTE. LTD.",
                        bank_name="DBS Bank Ltd", signed=False))
    assert any(x.rule_id == "BNK-024" for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_bank_statement_missing_bank_name_warns():
    f = run_rules(_bank("bank_statement", account_holder="IQH LABS PTE. LTD.",
                        account_number="072-154506-3", signed=False))
    assert any(x.rule_id == "BNK-025" for x in f)
    assert decide(f) == "WARNING"


def test_payment_instructions_missing_holder_is_nmr():
    f = run_rules(_bank("payment_instructions", bank_name="Chase",
                        account_number="12345678"))
    assert any(x.rule_id == "BNK-023" for x in f)
    assert any(x.rule_id == "BNK-004" for x in f)   # context warning still there
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_ap_document_self_certified_note():
    f = run_rules(_bank("ap_document", account_holder="タカキ ユウスケ",
                        bank_name="福岡銀行", account_number="1442667", signed=True))
    assert any(x.rule_id == "BNK-005" for x in f)
    assert decide(f) == "ACCEPT"


def test_us_numeric_iban_field_not_flagged():
    # a purely numeric value in the iban field (US, no IBAN) is a plain account
    # number, not a malformed IBAN — BNK-011 must NOT fire
    f = run_rules(_bank("payment_instructions", account_holder="Jamcorder LLC",
                        bank_name="Citizens", bank_country="US",
                        account_number="591564501132927", iban="591564501132927",
                        routing_aba="121145433", signed=False))
    assert not any(x.rule_id == "BNK-011" for x in f)


def test_payment_instructions_warn_not_reject():
    f = run_rules(_bank("payment_instructions", account_holder="CRH Management LLC",
                        bank_name="Intrust Bank", account_number="86601269",
                        routing_aba="101100029", signed=False))
    assert decide(f) == "WARNING"
    assert any(x.rule_id == "BNK-004" for x in f)
    assert not any(x.rule_id == "BNK-001" for x in f)


def test_masked_values_in_messages():
    f = run_rules(_bank("bank_letter", account_holder="ACME", bank_name="DB",
                        bank_country="DE", iban="DE4450010517540732493"))
    msg = next(x.message for x in f if x.rule_id == "BNK-011")
    assert "DE4450010517540732493" not in msg
    assert "DE**…" in msg


def test_w8_detected_needs_review():
    f = run_rules(_w9("w8"))
    assert decide(f) == "NEED_MANUAL_REVIEW"
    assert any(x.rule_id == "W9-030" for x in f)


def test_w9_individual_llc_ein_mix():
    f = run_rules(_w9("w9", line1_name="John Smith LLC", line3_classification="Individual/sole proprietor",
                      tin_type="EIN", tin_raw="12-3456789"))
    assert any(x.rule_id == "W9-012" for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_w9_clean_individual_accepts():
    f = run_rules(_w9("w9", line1_name="John Smith",
                      line3_classification="Individual/sole proprietor",
                      tin_type="SSN", tin_raw="320-54-0693",
                      address_street="1 Main St", address_city_state_zip="Chicago, IL 60606"))
    assert decide(f) == "ACCEPT"


def test_w9_ein_wrong_digits():
    f = run_rules(_w9("w9", line1_name="ACME LLC", line3_classification="LLC",
                      tin_type="EIN", tin_raw="12-345678"))  # 8 digits
    assert any(x.rule_id == "W9-010" for x in f)


def test_w9_line_swap_suspect():
    f = run_rules(_w9("w9", line1_name="", line2_business_name="ACME LLC",
                      tin_type="EIN", tin_raw="12-3456789", line3_classification="LLC"))
    assert any(x.rule_id == "W9-013" for x in f)


def test_parse_date_day_first_abbrev():
    """audit C13: '15 Jan 2023' fell to the year-only fallback (-> July 1)
    while the ABAP twin parsed it — BNK-020 fired on one side only."""
    from datetime import datetime

    from mdmdoc.rules.predicates import parse_date
    assert parse_date("15 Jan 2023") == datetime(2023, 1, 15)
    assert parse_date("15 Jan, 2023") == datetime(2023, 1, 15)
    assert parse_date("15 January, 2023") == datetime(2023, 1, 15)
    assert parse_date("Jan 15 2023") == datetime(2023, 1, 15)
    assert parse_date("January 15 2023") == datetime(2023, 1, 15)
    # existing behaviors unchanged
    assert parse_date("15 January 2023") == datetime(2023, 1, 15)
    assert parse_date("Jan 15, 2023") == datetime(2023, 1, 15)
    assert parse_date("3 de enero de 2024") == datetime(2024, 1, 3)


def test_date_older_than_injectable_clock(monkeypatch):
    from mdmdoc.rules.predicates import date_older_than
    monkeypatch.setenv("MDMDOC_NOW", "2026-07-09")
    fired, detail = date_older_than("15 Jan 2023", {}, {"years": 2}, {})
    assert fired and "2023-01-15" in detail
    fired, _ = date_older_than("15 Jan 2025", {}, {"years": 2}, {})
    assert not fired
    monkeypatch.setenv("MDMDOC_NOW", "not-a-date")   # invalid -> falls back, no raise
    fired, _ = date_older_than("15 Jan 2019", {}, {"years": 2}, {})
    assert fired


def test_bad_rule_fails_closed_to_nmr(tmp_path, monkeypatch):
    import yaml
    from mdmdoc import config
    bad = {"version": 1, "tables": {},
           "rules": [{"id": "X-1", "when": {"check": "nonexistent_predicate"},
                      "severity": "WARNING", "message": "x"}]}
    (tmp_path / "banking.yaml").write_text(yaml.safe_dump(bad))
    (tmp_path / "w9.yaml").write_text(yaml.safe_dump({"version": 1, "rules": []}))
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    f = run_rules(_bank("bank_letter"))
    assert any("engine_error" in x.message for x in f)
    assert any(x.rule_id == "ENGINE-GUARD" for x in f)
    # an errored rule might have been a REJECT — fail closed, never ACCEPT
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_invalid_verdict_effect_fails_closed(tmp_path, monkeypatch):
    import yaml
    from mdmdoc import config
    bad = {"version": 1, "tables": {},
           "rules": [{"id": "X-2", "when": {"always": True},
                      "severity": "CRITICAL", "verdict_effect": "REJCT",
                      "message": "typo'd effect"}]}
    (tmp_path / "banking.yaml").write_text(yaml.safe_dump(bad))
    (tmp_path / "w9.yaml").write_text(yaml.safe_dump({"version": 1, "rules": []}))
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    f = run_rules(_bank("bank_letter"))
    own = [x for x in f if x.rule_id == "X-2"]
    assert own and own[0].verdict_effect is None  # approved text not mutated
    assert any(x.rule_id == "ENGINE-GUARD" and "invalid verdict_effect" in x.message
               for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"


# --- W9-040/041 TIN structure & placeholder (us-tax-number-validator port) ---

def test_tin_structural_valid_shapes_quiet():
    from mdmdoc.rules.predicates import tin_structural
    assert tin_structural("36-1234567", {}, {}, {}) == (False, "")   # valid EIN prefix
    assert tin_structural("320-54-0693", {}, {}, {}) == (False, "")  # valid SSN
    assert tin_structural("912-70-1234", {}, {}, {}) == (False, "")  # ITIN group 70-88
    assert tin_structural("912-93-1234", {}, {}, {}) == (False, "")  # ATIN group 93


def test_tin_structural_ein_never_prefix():
    from mdmdoc.rules.predicates import tin_structural
    fired, detail = tin_structural("07-1234567", {}, {}, {})
    assert fired and detail == "EIN prefix is not an IRS-assigned prefix"


def test_tin_structural_ssn_components():
    from mdmdoc.rules.predicates import tin_structural
    assert tin_structural("000-12-3456", {}, {}, {}) == (True, "SSN area is a never-issued area")
    assert tin_structural("666-12-3456", {}, {}, {}) == (True, "SSN area is a never-issued area")
    assert tin_structural("123-00-4567", {}, {}, {}) == (True, "SSN group is invalid")
    assert tin_structural("123-45-0000", {}, {}, {}) == (True, "SSN serial is invalid")


def test_tin_structural_itin_gap_group():
    from mdmdoc.rules.predicates import tin_structural
    fired, detail = tin_structural("912-89-1234", {}, {}, {})  # 89 in the 89-gap
    assert fired and detail == "ITIN group is outside the IRS-assigned ranges"


def test_tin_structural_bare_nine_any_of():
    from mdmdoc.rules.predicates import tin_structural
    # 960891234: EIN prefix 96 dead; starts with 9 -> ITIN path, group 89 in the gap
    fired, detail = tin_structural("960891234", {}, {}, {})
    assert fired and detail == "9 digits match no valid EIN/SSN/ITIN structure"
    # 212554321: EIN prefix 21 is valid -> any-of passes, quiet
    assert tin_structural("212554321", {}, {}, {}) == (False, "")


def test_tin_structural_type_hint_narrows():
    from mdmdoc.rules.predicates import tin_structural
    # bare digits, dead EIN prefix 07 but valid as SSN 071-23-4567: hint decides
    fired, _ = tin_structural("071234567", {"tin_type": "EIN"}, {}, {})
    assert fired  # EIN hint -> dead prefix
    assert tin_structural("071234567", {"tin_type": ""}, {}, {}) == (False, "")  # any-of: SSN ok


def test_tin_structural_ignores_non_nine():
    from mdmdoc.rules.predicates import tin_structural, tin_placeholder
    for v in ("12-345678", "", "Applied For", "1234567890"):
        assert tin_structural(v, {}, {}, {}) == (False, "")
        assert tin_placeholder(v, {}, {}, {}) == (False, "")


def test_tin_placeholder_fires():
    from mdmdoc.rules.predicates import tin_placeholder
    assert tin_placeholder("999999999", {}, {}, {}) == (True, "repeated-single-digit placeholder")
    assert tin_placeholder("000-00-0000", {}, {}, {}) == (True, "repeated-single-digit placeholder")
    assert tin_placeholder("078-05-1120", {}, {}, {}) == (True, "known never-issued / reserved example TIN")
    assert tin_placeholder("987654329", {}, {}, {}) == (True, "known never-issued / reserved example TIN")


def test_tin_predicates_disjoint_and_digit_free():
    import re
    from mdmdoc.rules.predicates import tin_placeholder, tin_structural
    assert tin_structural("999999999", {}, {}, {}) == (False, "")  # placeholder territory
    for v in ("07-1234567", "000-12-3456", "912-89-1234", "999999999", "078-05-1120"):
        for pred in (tin_structural, tin_placeholder):
            _, detail = pred(v, {}, {}, {})
            assert not re.search(r"\d{4,}", detail), f"digits leaked in detail: {detail!r}"


def test_w9_040_dead_prefix_nmr():
    f = run_rules(_w9("w9", line1_name="ACME LLC", line3_classification="LLC",
                      tin_type="EIN", tin_raw="07-1234567"))
    assert any(x.rule_id == "W9-040" for x in f)
    assert not any(x.rule_id in ("W9-041", "W9-010") for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"
    msg = next(x.message for x in f if x.rule_id == "W9-040")
    assert "07-1234567" not in msg  # masked


def test_w9_041_placeholder_nmr():
    f = run_rules(_w9("w9", line1_name="ACME LLC", line3_classification="LLC",
                      tin_type="EIN", tin_raw="99-9999999"))
    assert any(x.rule_id == "W9-041" for x in f)
    assert not any(x.rule_id == "W9-040" for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_w9_clean_ein_still_accepts():
    f = run_rules(_w9("w9", line1_name="ACME LLC", line3_classification="LLC",
                      tin_type="EIN", tin_raw="36-1234567",
                      address_street="1 Main St", address_city_state_zip="Chicago, IL 60606"))
    assert decide(f) == "ACCEPT"
    assert not any(x.rule_id in ("W9-040", "W9-041") for x in f)


def test_w9_wrong_digit_count_stays_w9_010_only():
    f = run_rules(_w9("w9", line1_name="ACME LLC", line3_classification="LLC",
                      tin_type="EIN", tin_raw="12-345678"))  # 8 digits
    assert any(x.rule_id == "W9-010" for x in f)
    assert not any(x.rule_id in ("W9-040", "W9-041") for x in f)


def test_w9_040_not_applied_to_w8():
    f = run_rules(_w9("w8", tin_raw="07-1234567"))
    assert not any(x.rule_id in ("W9-040", "W9-041") for x in f)


def test_w9_041_joins_w9_012_on_blocklisted_tin():
    # the W9-012 triple uses 12-3456789 (= blocklisted 123456789): both fire, NMR
    f = run_rules(_w9("w9", line1_name="John Smith LLC",
                      line3_classification="Individual/sole proprietor",
                      tin_type="EIN", tin_raw="12-3456789"))
    assert any(x.rule_id == "W9-012" for x in f)
    assert any(x.rule_id == "W9-041" for x in f)
    assert decide(f) == "NEED_MANUAL_REVIEW"
