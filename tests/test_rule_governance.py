"""audit-wave C8: the rule-authoring path refuses rules that would fail closed
at runtime (unknown predicate, bad placeholder, typo'd effect) — save_rules now
runs the full semantic validation, closing the ENGINE-GUARD authoring path."""
import pytest
import yaml

from mdmdoc import config, rules_io
from mdmdoc.rule_propose import validate_rule


@pytest.fixture()
def rules_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    return tmp_path


def _doc(rule: dict, doc_types=("bank_letter", "invoice")) -> str:
    return yaml.safe_dump({"version": 1, "doc_types": list(doc_types),
                           "tables": {}, "rules": [rule]})


BASE = {"id": "X-1", "name": "t", "when": {"always": True},
        "severity": "WARNING", "message": "ok"}


def test_save_rejects_unknown_predicate(rules_env):
    bad = dict(BASE, when={"check": "does_not_exist"})
    with pytest.raises(ValueError, match="does not exist"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_rejects_unknown_placeholder(rules_env):
    bad = dict(BASE, message="amount is {amount}")
    with pytest.raises(ValueError, match="unknown placeholder"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_rejects_positional_placeholder(rules_env):
    bad = dict(BASE, message="value: {}")
    with pytest.raises(ValueError, match="positional"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_rejects_malformed_braces(rules_env):
    bad = dict(BASE, message="regex {\\d+ unclosed")
    with pytest.raises(ValueError, match="malformed"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_rejects_invalid_verdict_effect(rules_env):
    bad = dict(BASE, verdict_effect="REJCT")
    with pytest.raises(ValueError, match="verdict_effect"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_rejects_unknown_doc_type_in_saved_file(rules_env):
    # applies_to validated against the file BEING SAVED, not the on-disk one
    bad = dict(BASE, applies_to=["hologram"])
    with pytest.raises(ValueError, match="unknown doc_type"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_accepts_doc_type_new_in_saved_file(rules_env):
    ok = dict(BASE, applies_to=["hologram"])
    n = rules_io.save_rules("bank", _doc(ok, doc_types=("hologram",)))
    assert n == 1


def test_save_rejects_ambiguous_when(rules_env):
    bad = dict(BASE, when={"flag_true": "signed", "field_missing": "iban"})
    with pytest.raises(ValueError, match="ambiguous"):
        rules_io.save_rules("bank", _doc(bad))


def test_save_rejects_non_mapping_rule(rules_env):
    text = yaml.safe_dump({"version": 1, "doc_types": [], "rules": ["not a rule"]})
    with pytest.raises(ValueError, match="not a mapping"):
        rules_io.save_rules("bank", text)


def test_validate_rule_when_op_not_dict_order_dependent():
    # companion keys (field/args) around `check` must not confuse detection
    r = dict(BASE, when={"field": "iban", "check": "iban_valid", "args": {}})
    assert validate_rule(r, "bank", known_doc_types=set()) == []


def test_shipped_rules_pass_save_verbatim(rules_env):
    """Regression guard: both shipped SECTIONS must save byte-identical."""
    import mdmdoc.rules_io as real_io
    shipped = __import__("pathlib").Path(__file__).resolve().parents[1] / "rules" / "rules.yaml"
    sections = real_io._split_sections(shipped.read_text(encoding="utf-8"))
    for doc_class in ("bank", "w9"):
        text = sections[doc_class]
        n = rules_io.save_rules(doc_class, text)
        assert n > 0
        assert rules_io.rules_text(doc_class) == text
