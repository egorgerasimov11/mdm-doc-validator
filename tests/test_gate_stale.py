"""A verdict is a persisted artifact. Approving a rule does not re-decide runs
that are already on disk, so a run held by RULE-GATE keeps reporting 'awaiting
approval' forever. The run page has to say that out loud, or the operator
concludes the approvals never took."""
import pytest
import yaml

from mdmdoc import config, rule_approvals, rules_io
from mdmdoc.server.ui import _gate_is_stale

GATED = [{"rule_id": "RULE-GATE", "severity": "WARNING",
          "message": "2 rule(s) await your approval"}]
CLEAN = [{"rule_id": "BNK-021", "severity": "WARNING", "message": "unsigned"}]


@pytest.fixture()
def rules_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "banking.yaml").write_text(yaml.safe_dump({
        "version": 1, "doc_types": ["bank_letter"], "tables": {},
        "rules": [{"id": "BNK-001", "name": "a", "tier": "corp", "applies_to": ["bank_letter"],
                   "when": {"always": True}, "severity": "NOTE",
                   "verdict_effect": None, "message": "a"}]}))
    return tmp_path


def _approve_all(doc_class="bank"):
    cfg = yaml.safe_load(rules_io.rules_text(doc_class)) or {}
    for r in cfg.get("rules") or []:
        rule_approvals.set_decision(doc_class, r, rule_approvals.APPROVED)


def test_no_gate_finding_is_never_stale(rules_env):
    assert _gate_is_stale(CLEAN, "bank") is False
    assert _gate_is_stale([], "bank") is False


def test_gate_with_pending_rules_is_not_stale_yet(rules_env):
    """Nothing was approved: the finding is still the literal truth."""
    assert _gate_is_stale(GATED, "bank") is False


def test_gate_becomes_stale_once_every_rule_is_approved(rules_env):
    _approve_all()
    assert _gate_is_stale(GATED, "bank") is True


def test_unreadable_rules_do_not_crash_the_page(rules_env, monkeypatch):
    monkeypatch.setattr(rules_io, "rules_text",
                        lambda dc: (_ for _ in ()).throw(OSError("gone")))
    assert _gate_is_stale(GATED, "bank") is False
