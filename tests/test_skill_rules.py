"""Tests for the skill-rules parser (skill dynamic_rules.md -> normalized list)."""
from mdmdoc import skill_rules as sr

FIXTURE = """# Dynamic Rules

Auto-managed overlay for this skill.

<!-- DYNAMIC-RULES START -->

### DR-20260705-170537 — W-9 reconciliation surfaces DO corrections as warnings
- Status: ACTIVE
- Severity: WARNING
- Action: MODIFY
- Origin: correction

Rule:
Surface the DO payment-field correction as a WARNING and route it to the form owner.
Do not make Z045 or A-ACH defaults.

Reason:
Keeps W-9 validation architecture clean.

---

### DR-20260608-121112 — EIN must contain exactly 9 digits
- Status: ACTIVE
- Severity: CRITICAL
- Action: ADD

Rule:
An EIN must contain exactly nine digits.

Reason:
Malformed TIN.

---

### DR-20260705-170307 — superseded rule
- Status: RETIRED
- Severity: CRITICAL
- Action: ADD

Rule:
Old wording.
"""


def test_parse_and_active(tmp_path):
    p = tmp_path / "dynamic_rules.md"
    p.write_text(FIXTURE)

    allr = sr.parse_dynamic_rules(p)
    assert len(allr) == 3
    ids = [r["id"] for r in allr]
    assert ids == ["DR-20260705-170537", "DR-20260608-121112", "DR-20260705-170307"]

    ein = next(r for r in allr if r["id"] == "DR-20260608-121112")
    assert ein["severity"] == "CRITICAL"
    assert ein["status"] == "ACTIVE"
    assert "nine digits" in ein["rule"]
    assert ein["reason"] == "Malformed TIN."
    assert "W9-010" in ein["coverage"]   # curated coverage map

    active = sr.active_rules(p)
    assert len(active) == 2
    assert all(r["status"] == "ACTIVE" for r in active)
    assert "DR-20260705-170307" not in [r["id"] for r in active]


def test_rule_block_stops_at_reason(tmp_path):
    p = tmp_path / "dynamic_rules.md"
    p.write_text(FIXTURE)
    warn = next(r for r in sr.parse_dynamic_rules(p) if r["id"] == "DR-20260705-170537")
    assert "route it to the form owner" in warn["rule"]
    assert "Reason" not in warn["rule"]
    assert warn["action"] == "MODIFY"
