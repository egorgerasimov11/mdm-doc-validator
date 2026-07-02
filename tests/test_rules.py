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


def test_bad_rule_never_crashes(tmp_path, monkeypatch):
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
    assert decide(f) == "ACCEPT"  # engine errors are NOTE, not verdicts
