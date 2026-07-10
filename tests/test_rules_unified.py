"""D9: the ONE physical rules file — multi-document sections keep every rule
block byte-identical (approval hashes survive), the section/save layer feeds
every consumer, the legacy two-file layout still works as a fallback."""
import yaml

from mdmdoc import config, rules_io
from mdmdoc.rule_approvals import rule_hash

BANK_YAML = """version: 1
doc_types: [bank_letter]
tables: {}
rules:
  # governance note that must survive round-trips
  - id: BNK-001
    name: one
    tier: corp
    applies_to: [bank_letter]
    when: {always: true}
    severity: NOTE
    verdict_effect: null
    message: "note"
"""
W9_YAML = """version: 1
doc_types: [w9]
tables: {}
rules:
  - id: W9-001
    name: line1_missing
    applies_to: [w9]
    when: {field_missing: line1_name}
    severity: CRITICAL
    verdict_effect: NEED_MANUAL_REVIEW
    message: "missing"
"""


def _unified(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    text = (rules_io._HEADER
            + "\n--- # doc_class: bank\n" + BANK_YAML
            + "\n--- # doc_class: w9\n" + W9_YAML)
    (tmp_path / "rules.yaml").write_text(text)
    return text


def test_sections_roundtrip_byte_identical(tmp_path, monkeypatch):
    _unified(tmp_path, monkeypatch)
    assert rules_io.rules_text("bank").strip() == BANK_YAML.strip()
    assert rules_io.rules_text("w9").strip() == W9_YAML.strip()
    # hashes identical to the standalone-file parse -> approvals survive
    for dc, legacy in (("bank", BANK_YAML), ("w9", W9_YAML)):
        old = {r["id"]: rule_hash(r) for r in yaml.safe_load(legacy)["rules"]}
        new = {r["id"]: rule_hash(r)
               for r in yaml.safe_load(rules_io.rules_text(dc))["rules"]}
        assert old == new


def test_engine_loads_from_unified(tmp_path, monkeypatch):
    _unified(tmp_path, monkeypatch)
    from mdmdoc.rules.engine import load_rules
    assert [r["id"] for r in load_rules("bank")["rules"]] == ["BNK-001"]
    assert [r["id"] for r in load_rules("w9")["rules"]] == ["W9-001"]


def test_save_rules_writes_section_preserving_others(tmp_path, monkeypatch):
    _unified(tmp_path, monkeypatch)
    new_bank = BANK_YAML + """
  - id: BNK-002
    name: two
    applies_to: [bank_letter]
    when: {field_missing: currency}
    severity: NOTE
    verdict_effect: null
    message: "currency"
"""
    assert rules_io.save_rules("bank", new_bank) == 2
    assert "BNK-002" in rules_io.rules_text("bank")
    assert rules_io.rules_text("w9").strip() == W9_YAML.strip()   # untouched
    assert "# governance note that must survive round-trips" \
        in rules_io.rules_text("bank")                            # comments live


def test_set_rule_tier_surgical_in_unified(tmp_path, monkeypatch):
    _unified(tmp_path, monkeypatch)
    out = rules_io.set_rule_tier("bank", "BNK-001", "experimental")
    assert out["old_tier"] == "corp" and out["hash_unchanged"]
    assert "tier: experimental" in rules_io.rules_text("bank")
    assert rules_io.rules_text("w9").strip() == W9_YAML.strip()


def test_legacy_two_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    (tmp_path / "banking.yaml").write_text(BANK_YAML)
    (tmp_path / "w9.yaml").write_text(W9_YAML)
    assert "BNK-001" in rules_io.rules_text("bank")
    assert rules_io.save_rules("bank", BANK_YAML) == 1            # legacy write
    assert not (tmp_path / "rules.yaml").exists()


def test_save_unified_validates_every_section(tmp_path, monkeypatch):
    text = _unified(tmp_path, monkeypatch)
    counts = rules_io.save_unified(text)
    assert counts == {"bank": 1, "w9": 1}
    import pytest
    with pytest.raises(ValueError):
        rules_io.save_unified(text.replace("severity: CRITICAL", "severity: BOGUS"))
    with pytest.raises(ValueError):
        rules_io.save_unified("no sections at all")
