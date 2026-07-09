"""F5 foundation: governance metadata (tier/source) is hash-immune — annotating
a rule must NOT invalidate an existing human approval (denylist hash → zero
migration of the mini's approvals.json). Changing verdict behaviour still does."""
from mdmdoc import rule_approvals as ra
from mdmdoc.rule_propose import apply_change


def test_tier_source_do_not_change_the_hash():
    base = {"id": "BNK-001", "when": {"always": True}, "severity": "CRITICAL",
            "verdict_effect": "REJECT", "message": "no"}
    h0 = ra.rule_hash(base)
    assert ra.rule_hash({**base, "tier": "corp"}) == h0
    assert ra.rule_hash({**base, "tier": "corp", "source": "policy"}) == h0


def test_verdict_change_still_invalidates():
    base = {"id": "BNK-001", "when": {"always": True}, "severity": "CRITICAL",
            "verdict_effect": "REJECT", "message": "no"}
    assert ra.rule_hash({**base, "verdict_effect": "WARNING"}) != ra.rule_hash(base)
    assert ra.rule_hash({**base, "message": "changed"}) != ra.rule_hash(base)


def test_status_survives_metadata_annotation(tmp_path, monkeypatch):
    monkeypatch.setattr(ra.config, "RULES_DIR", tmp_path)
    rule = {"id": "BNK-020", "when": {"always": True}, "severity": "NOTE",
            "verdict_effect": None, "message": "note"}
    ra.set_decision("bank", rule, ra.APPROVED)
    store = ra.load()
    assert ra.status(store, "bank", rule) == ra.APPROVED
    # annotate with governance metadata — approval must hold
    assert ra.status(store, "bank", {**rule, "tier": "corp", "source": "skill"}) == ra.APPROVED


def test_edit_proposal_carries_tier_source():
    text = (
        "rules:\n"
        "  - id: BNK-020\n"
        "    when: {always: true}\n"
        "    severity: NOTE\n"
        "    verdict_effect: null\n"
        "    message: old\n"
        "    tier: experimental\n"
        "    source: skill\n"
    )
    # model proposes an edit WITHOUT the metadata keys
    edited = apply_change(text, "edit",
                          {"id": "BNK-020", "when": {"always": True}, "severity": "NOTE",
                           "verdict_effect": None, "message": "new wording"})
    import yaml
    r = yaml.safe_load(edited)["rules"][0]
    assert r["message"] == "new wording"
    assert r["tier"] == "experimental" and r["source"] == "skill"
