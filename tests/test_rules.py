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
