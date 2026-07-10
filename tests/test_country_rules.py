"""F3: country-scoped rules — `countries: [DE]` gates a rule on the document's
derived country; an undetected country SKIPS the rule with one COUNTRY-1 NOTE
(operator decision: inform, never block)."""
import pytest
import yaml

from mdmdoc import config, rule_approvals, rules_io
from mdmdoc.fields import Extraction
from mdmdoc.rules.engine import run_rules
from mdmdoc.stage_b import _ground_doc_country
from mdmdoc.verdict import decide

RULES = {
    "version": 1,
    "doc_types": ["bank_letter"],
    "tables": {},
    "rules": [
        {"id": "BNK-C01", "name": "de only (test)", "countries": ["DE"],
         "applies_to": ["bank_letter"], "when": {"always": True},
         "severity": "WARNING", "verdict_effect": "NEED_MANUAL_REVIEW",
         "message": "de-scoped"},
        {"id": "BNK-C02", "name": "everywhere (test)",
         "applies_to": ["bank_letter"], "when": {"always": True},
         "severity": "NOTE", "verdict_effect": None, "message": "global"},
    ],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "banking.yaml").write_text(yaml.safe_dump(RULES, sort_keys=False))
    return tmp_path


def _ext(**fields):
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields.update(fields)
    return e


# ---------------------------------------------------------------- engine ------
def test_country_rule_fires_on_matching_country(env):
    fired = {f.rule_id for f in run_rules(_ext(doc_country="DE"))}
    assert "BNK-C01" in fired and "BNK-C02" in fired
    assert "COUNTRY-1" not in fired


def test_country_rule_silent_on_other_country(env):
    trace: list = []
    findings = run_rules(_ext(doc_country="US"), trace=trace)
    fired = {f.rule_id for f in findings}
    assert "BNK-C01" not in fired and "COUNTRY-1" not in fired
    row = next(t for t in trace if t["rule_id"] == "BNK-C01")
    assert row["outcome"] == "not-applicable-country"


def test_unknown_country_skips_with_note(env):
    findings = run_rules(_ext())
    by_id = {f.rule_id: f for f in findings}
    assert "BNK-C01" not in by_id                       # skipped, not fired
    note = by_id["COUNTRY-1"]
    assert note.severity == "NOTE" and note.verdict_effect is None
    assert "BNK-C01" in note.message
    assert decide(findings) == "ACCEPT"                 # the NOTE never blocks


def test_unknown_country_does_not_hold_rule_gate(env):
    # BNK-C01 is PENDING; with the country unknown it is SKIPPED before the
    # approval gate — only the global rule holds the gate
    pending: list = []
    findings = run_rules(_ext(), enforce_approvals=True, pending_out=pending)
    assert {p["id"] for p in pending} == {"BNK-C02"}
    gate = next(f for f in findings if f.rule_id == "RULE-GATE")
    assert "BNK-C01" not in gate.message


def test_wrong_country_does_not_hold_rule_gate(env):
    pending: list = []
    run_rules(_ext(doc_country="US"), enforce_approvals=True, pending_out=pending)
    assert {p["id"] for p in pending} == {"BNK-C02"}


def test_matching_country_pending_rule_holds_gate(env):
    pending: list = []
    run_rules(_ext(doc_country="DE"), enforce_approvals=True, pending_out=pending)
    assert {p["id"] for p in pending} == {"BNK-C01", "BNK-C02"}


def test_lowercase_country_in_rule_still_matches(env, tmp_path):
    text = rules_io.rules_text("bank").replace("- DE", "- de")
    (tmp_path / "rules" / "banking.yaml").write_text(text)
    fired = {f.rule_id for f in run_rules(_ext(doc_country="DE"))}
    assert "BNK-C01" in fired


# ---------------------------------------------------------------- guard -------
def test_ground_doc_country_priority_bank():
    e = _ext(bank_country="Germany", iban="FR7630006000011234567890189",
             swift_bic="DEUTUSX9")
    _ground_doc_country(e, None)
    assert e.fields["doc_country"] == "DE"              # bank_country wins
    e = _ext(iban="FR7630006000011234567890189", swift_bic="DEUTUSX9")
    _ground_doc_country(e, None)
    assert e.fields["doc_country"] == "FR"              # then IBAN prefix
    e = _ext(swift_bic="DEUTUSX9")
    _ground_doc_country(e, None)
    assert e.fields["doc_country"] == "US"              # then SWIFT cc
    e = _ext()
    _ground_doc_country(e, None)
    assert "doc_country" not in e.fields                # nothing to ground


def test_ground_doc_country_never_overwrites():
    e = _ext(doc_country="IT", bank_country="Germany")
    _ground_doc_country(e, None)
    assert e.fields["doc_country"] == "IT"


def test_ground_doc_country_w9_and_w8():
    e = Extraction(doc_class="w9", doc_type="w9")
    _ground_doc_country(e, None)
    assert e.fields["doc_country"] == "US"              # a W-9 is a US form
    e = Extraction(doc_class="w9", doc_type="w8")
    e.fields["country_incorporation"] = "Italy"
    _ground_doc_country(e, None)
    assert e.fields["doc_country"] == "IT"
    e = Extraction(doc_class="w9", doc_type="other_tax")
    _ground_doc_country(e, None)
    assert "doc_country" not in e.fields


# ---------------------------------------------------------------- authoring ---
def test_save_rules_accepts_and_validates_countries(env):
    text = rules_io.rules_text("bank")
    assert rules_io.save_rules("bank", text) == 2       # round-trips verbatim
    bad = text.replace("- DE", "- DEUTSCHLAND")
    with pytest.raises(ValueError, match="countries"):
        rules_io.save_rules("bank", bad)
