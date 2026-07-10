"""D10: skills as rule sources — deterministic import for checker skills,
strong-model extraction for arbitrary text, everything PENDING, re-import
replaces only that skill's rules, other sources byte-untouched."""
import yaml

import pytest

from mdmdoc import config, rules_io, skill_import

BANK_YAML = """version: 1
doc_types: [bank_letter, payment_instructions]
tables: {}
rules:
  # a hand-written comment that must survive skill imports
  - id: BNK-001
    name: existing
    tier: corp
    source: policy
    applies_to: [bank_letter]
    when: {always: true}
    severity: NOTE
    verdict_effect: null
    message: "existing"
"""

DYNAMIC_RULES = """# dynamic rules
### DR-20260101-000001 — Bank letters must name the account holder
- Status: active
- Severity: high
- Action: kickback
- Scope: bank documents

Rule:
The bank letter must explicitly name the account holder.

Reason:
unnamed letters are not attributable.

### DR-20260101-000002 — Retired rule
- Status: retired
- Severity: low

Rule:
obsolete.
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "banking.yaml").write_text(BANK_YAML)
    return tmp_path


def test_checker_skill_imports_deterministically(env):
    root = skill_import.store_upload("mdm-banking-checker", "dynamic_rules.md",
                                     DYNAMIC_RULES.encode())
    # stored under references-like layout? single file is fine — rglob finds it
    out = skill_import.import_skill("mdm-banking-checker", "bank",
                                    log=lambda *_: None)
    assert out["imported"] == 1                       # retired entry skipped
    text = rules_io.rules_text("bank")
    cfg = yaml.safe_load(text)
    imported = [r for r in cfg["rules"]
                if str(r.get("source", "")).startswith("skill:")]
    assert len(imported) == 1
    r = imported[0]
    assert r["source"] == "skill:mdm-banking-checker"
    assert r["tier"] == "experimental"
    assert r["severity"] == "CRITICAL"
    assert r["verdict_effect"] is None                # advisory until approved logic
    assert "DR-20260101-000001" in r["message"]
    # untouched neighbors: comment + existing rule bytes
    assert "# a hand-written comment that must survive skill imports" in text
    # the imported rule is PENDING -> gate holds it
    from mdmdoc import rule_approvals
    assert rule_approvals.status(rule_approvals.load(), "bank", r) \
        == rule_approvals.PENDING


def test_reimport_replaces_own_rules_only(env):
    skill_import.store_upload("mdm-banking-checker", "dynamic_rules.md",
                              DYNAMIC_RULES.encode())
    skill_import.import_skill("mdm-banking-checker", "bank", log=lambda *_: None)
    # second import: same skill, updated content
    updated = DYNAMIC_RULES.replace("must explicitly name", "must clearly identify")
    skill_import.store_upload("mdm-banking-checker", "dynamic_rules.md",
                              updated.encode())
    out = skill_import.import_skill("mdm-banking-checker", "bank",
                                    log=lambda *_: None)
    assert out["replaced"] == 1 and out["imported"] == 1
    cfg = yaml.safe_load(rules_io.rules_text("bank"))
    mine = [r for r in cfg["rules"]
            if str(r.get("source", "")) == "skill:mdm-banking-checker"]
    assert len(mine) == 1
    assert "clearly identify" in mine[0]["message"]
    assert any(r["id"] == "BNK-001" for r in cfg["rules"])   # others intact


def test_arbitrary_skill_goes_through_model_and_validation(env, monkeypatch):
    skill_import.store_upload("my-notes", "SKILL.md",
                              b"Always require a currency on bank letters.")
    fake_rules = {"rules": [
        {"id": "BNK-901", "name": "currency_missing",
         "applies_to": ["bank_letter"],
         "when": {"field_missing": "currency"}, "severity": "NOTE",
         "verdict_effect": None, "message": "currency missing"},
        {"id": "BNK-902", "name": "bogus", "applies_to": ["bank_letter"],
         "when": {"check": "nonexistent_predicate"}, "severity": "NOTE",
         "verdict_effect": None, "message": "x"},
    ]}
    from mdmdoc import model_client as mc
    monkeypatch.setattr(mc, "generate_json", lambda *a, **k: (fake_rules, True))
    monkeypatch.setattr(mc, "unload", lambda role: None)
    out = skill_import.import_skill("my-notes", "bank", log=lambda *_: None)
    assert out["imported"] == 1                       # bogus predicate dropped
    cfg = yaml.safe_load(rules_io.rules_text("bank"))
    assert any(r["id"] == "BNK-901" for r in cfg["rules"])
    assert not any(r["id"] == "BNK-902" for r in cfg["rules"])


def test_list_imported_counts(env):
    skill_import.store_upload("mdm-banking-checker", "dynamic_rules.md",
                              DYNAMIC_RULES.encode())
    skill_import.import_skill("mdm-banking-checker", "bank", log=lambda *_: None)
    rows = skill_import.list_imported()
    assert rows and rows[0]["name"] == "mdm-banking-checker"
    assert rows[0]["rule_count"] == 1
