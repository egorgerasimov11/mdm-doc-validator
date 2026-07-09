"""Hard approval gate: a rule fires only after a human Approves it; a pending
applicable rule holds the run at NEED_MANUAL_REVIEW; a rejected rule is skipped;
editing an approved rule reverts it to pending."""
import pytest
import yaml

from mdmdoc import config, rule_approvals
from mdmdoc.fields import Extraction
from mdmdoc.rules.engine import run_rules
from mdmdoc.verdict import decide

MINI_RULES = {
    "version": 1,
    "doc_types": ["invoice", "bank_letter"],
    "rules": [
        {"id": "T-1", "name": "always_reject_invoice", "applies_to": ["invoice"],
         "when": {"always": True}, "severity": "CRITICAL", "verdict_effect": "REJECT",
         "message": "invoice not allowed"},
    ],
}


@pytest.fixture()
def rules_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "banking.yaml").write_text(yaml.safe_dump(MINI_RULES))
    return tmp_path


def _ext():
    return Extraction(doc_class="bank", doc_type="invoice")


def _rule():
    return MINI_RULES["rules"][0]


def test_hash_stable_and_status_transitions(rules_env):
    store = rule_approvals.load()
    assert rule_approvals.status(store, "bank", _rule()) == rule_approvals.PENDING
    rule_approvals.set_decision("bank", _rule(), "approved")
    assert rule_approvals.status(rule_approvals.load(), "bank", _rule()) == rule_approvals.APPROVED
    # editing the rule text invalidates the approval
    changed = dict(_rule(), message="different text")
    assert rule_approvals.status(rule_approvals.load(), "bank", changed) == rule_approvals.PENDING


def test_gate_off_fires_raw(rules_env):
    f = run_rules(_ext(), enforce_approvals=False)
    assert any(x.rule_id == "T-1" for x in f) and decide(f) == "REJECT"


def test_gate_pending_holds_manual_review(rules_env):
    f = run_rules(_ext(), enforce_approvals=True)
    assert not any(x.rule_id == "T-1" for x in f)          # un-approved rule does NOT fire
    assert any(x.rule_id == "RULE-GATE" for x in f)         # gate flags it
    assert decide(f) == "NEED_MANUAL_REVIEW"                # never silently ACCEPT


def test_gate_approved_fires(rules_env):
    rule_approvals.set_decision("bank", _rule(), "approved")
    f = run_rules(_ext(), enforce_approvals=True)
    assert any(x.rule_id == "T-1" for x in f)
    assert not any(x.rule_id == "RULE-GATE" for x in f)
    assert decide(f) == "REJECT"


def test_gate_rejected_skips_silently(rules_env):
    rule_approvals.set_decision("bank", _rule(), "rejected")
    f = run_rules(_ext(), enforce_approvals=True)
    assert not any(x.rule_id == "T-1" for x in f)           # disabled on purpose
    assert not any(x.rule_id == "RULE-GATE" for x in f)     # rejected != needs review
    assert decide(f) == "ACCEPT"


def test_approved_then_edited_reverts_to_pending(rules_env):
    rule_approvals.set_decision("bank", _rule(), "approved")
    # simulate the operator editing the rule's message in the YAML
    edited = dict(MINI_RULES, rules=[dict(_rule(), message="edited")])
    (rules_env / "rules" / "banking.yaml").write_text(yaml.safe_dump(edited))
    f = run_rules(_ext(), enforce_approvals=True)
    assert any(x.rule_id == "RULE-GATE" for x in f)         # changed rule needs re-approval
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_corrupt_ledger_fails_closed_with_backup(rules_env):
    rule_approvals.set_decision("bank", _rule(), "approved")
    p = config.RULES_DIR / "approvals.json"
    original_garbage = "{not json at all"
    p.write_text(original_garbage, encoding="utf-8")
    assert rule_approvals.load() == {}                      # fail closed: all PENDING
    backup = p.with_name("approvals.json.corrupt")
    assert backup.read_text(encoding="utf-8") == original_garbage
    # a second, different corruption must NOT clobber the first snapshot
    p.write_text("other garbage", encoding="utf-8")
    assert rule_approvals.load() == {}
    assert backup.read_text(encoding="utf-8") == original_garbage
    # and the gate holds the run at NMR
    f = run_rules(_ext(), enforce_approvals=True)
    assert decide(f) == "NEED_MANUAL_REVIEW"


def test_concurrent_decisions_lose_nothing(rules_env):
    import threading
    rules = [dict(_rule(), id=f"T-{i}") for i in range(8)]
    threads = [threading.Thread(target=rule_approvals.set_decision,
                                args=("bank", r, "approved")) for r in rules]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store = rule_approvals.load()                           # parses (not torn)
    assert {f"bank:T-{i}" for i in range(8)} <= set(store)  # no update lost


def test_rejected_then_edited_reverts_to_pending(rules_env):
    rule_approvals.set_decision("bank", _rule(), "rejected")
    # rejection is bound to the exact text the human saw — a rewritten rule
    # under the same id must NOT inherit the old rejection (fail-open hole)
    edited_rule = dict(_rule(), when={"always": True}, message="rewritten check")
    assert rule_approvals.status(rule_approvals.load(), "bank", edited_rule) \
        == rule_approvals.PENDING
    edited = dict(MINI_RULES, rules=[edited_rule])
    (rules_env / "rules" / "banking.yaml").write_text(yaml.safe_dump(edited))
    f = run_rules(_ext(), enforce_approvals=True)
    assert any(x.rule_id == "RULE-GATE" for x in f)         # surfaces for review
    assert decide(f) == "NEED_MANUAL_REVIEW"
