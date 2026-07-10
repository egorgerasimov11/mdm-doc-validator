"""F2b: guided rule creation — the model drafts, a DETERMINISTIC
questionnaire (one question + exactly three buttons) settles the gaps, the
finished rule lands PENDING through the normal validated save path."""
import pytest
import yaml

from mdmdoc import config, rule_approvals, rule_wizard, rules_io

RULES = {
    "version": 1,
    "doc_types": ["bank_letter", "bank_statement", "invoice"],
    "tables": {},
    "rules": [
        {"id": "BNK-001", "name": "no_invoice", "tier": "corp",
         "applies_to": ["invoice"], "when": {"always": True},
         "severity": "CRITICAL", "verdict_effect": "REJECT",
         "message": "invoices are not bank support",
         "message_ru": "инвойс не является банковским подтверждением"},
        {"id": "BNK-047", "name": "no_statement", "tier": "corp",
         "applies_to": ["bank_statement"], "when": {"always": True},
         "severity": "CRITICAL", "verdict_effect": "REJECT",
         "message": "statements are not acceptable",
         "message_ru": "выписка не принимается"},
    ],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    (tmp_path / "rules").mkdir()
    # canonical file style: list items at 2 spaces, keys at 4 — the same shape
    # rule_propose._block_text emits, so add-splices stay style-consistent
    body = yaml.safe_dump({k: v for k, v in RULES.items() if k != "rules"},
                          sort_keys=False, allow_unicode=True)
    body += "rules:\n"
    for r in RULES["rules"]:
        block = yaml.safe_dump([r], sort_keys=False, allow_unicode=True,
                               default_flow_style=None, width=4096)
        body += "".join("  " + ln + "\n" for ln in block.splitlines())
    (tmp_path / "rules" / "banking.yaml").write_text(body)
    return tmp_path


def test_next_rule_id_skips_taken_numbers(env):
    assert rule_wizard.next_rule_id("bank") == "BNK-048"


def test_questions_cover_the_gaps(env):
    d = {"rule": {"name": "x", "when": {"always": True},
                  "message": "m", "message_ru": "м"}}
    qs = rule_wizard.questions(d, "bank")
    keys = [q["key"] for q in qs]
    assert keys == ["applies_to", "strictness"]
    assert all(len(q["options"]) == 3 for q in qs)      # Egor's shape: 3 buttons


def test_questions_confirm_a_drafted_country(env):
    d = {"rule": {"name": "x", "when": {"always": True}, "countries": ["DE"],
                  "verdict_effect": "REJECT", "applies_to": ["bank_statement"],
                  "message": "m", "message_ru": "м"}}
    qs = rule_wizard.questions(d, "bank")
    assert [q["key"] for q in qs] == ["countries"]
    assert qs[0]["options"][0] == "only DE"


def test_malformed_model_clarify_is_dropped(env):
    d = {"rule": {"name": "x", "when": {"always": True},
                  "applies_to": ["invoice"], "verdict_effect": None,
                  "message": "m", "message_ru": "м"},
         "clarify": {"question": "??", "options": ["a", "b"]}}   # 2 options
    assert all(q["key"] != "clarify" for q in rule_wizard.questions(d, "bank"))


def test_apply_answers_merges_and_defaults(env):
    rule = {"name": "reject_de_statements", "when": {"always": True},
            "countries": ["DE"], "message": "no statements for DE",
            "message_ru": "выписки для DE не принимаются"}
    merged = rule_wizard.apply_answers(
        rule, {"applies_to": "bank_statement",
               "strictness": "REJECT — refuse the document",
               "countries": "only DE"}, "bank")
    assert merged["applies_to"] == ["bank_statement"]
    assert merged["severity"] == "CRITICAL" and merged["verdict_effect"] == "REJECT"
    assert merged["countries"] == ["DE"]
    assert merged["id"] == "BNK-048"
    assert merged["tier"] == "experimental" and merged["source"] == "operator"


def test_all_types_answer_removes_the_scope(env):
    rule = {"name": "x", "when": {"always": True}, "message": "m",
            "message_ru": "м"}
    merged = rule_wizard.apply_answers(
        rule, {"applies_to": rule_wizard.ALL_TYPES,
               "strictness": "WARNING — flag but allow"}, "bank")
    assert "applies_to" not in merged
    assert merged["verdict_effect"] == "WARNING"


def test_create_appends_pending_and_rejects_invalid(env):
    rule = rule_wizard.apply_answers(
        {"name": "reject_de_statements", "when": {"always": True},
         "countries": ["DE"], "message": "no statements for DE",
         "message_ru": "выписки для DE не принимаются"},
        {"applies_to": "bank_statement",
         "strictness": "REJECT — refuse the document"}, "bank")
    out = rule_wizard.create(rule, "bank")
    assert out["ok"], out
    cfg = yaml.safe_load(rules_io.rules_text("bank"))
    created = next(r for r in cfg["rules"] if r["id"] == "BNK-048")
    assert created["countries"] == ["DE"]
    store = rule_approvals.load()
    assert rule_approvals.status(store, "bank", created) == rule_approvals.PENDING

    bad = dict(rule, id="BNK-049", when={"check": "no_such_predicate"})
    out2 = rule_wizard.create(bad, "bank")
    assert not out2["ok"] and out2["issues"]
    assert "BNK-049" not in rules_io.rules_text("bank")


def test_draft_survives_model_garbage(env, monkeypatch):
    from mdmdoc import model_client as mc
    monkeypatch.setattr(mc, "generate_json", lambda *a, **k: (None, False))
    monkeypatch.setattr(mc, "unload", lambda *a, **k: None)
    out = rule_wizard.draft("не принимать выписки", "bank")
    assert out["ok"] is False and "rephrase" in out["rationale"]


def test_draft_happy_path_builds_questions(env, monkeypatch):
    from mdmdoc import model_client as mc
    fake = {"rationale": "statements are rejected for DE",
            "rule": {"name": "reject_de_statements", "when": {"always": True},
                     "countries": ["DE"], "message": "no statements for DE",
                     "message_ru": "выписки для DE не принимаются"},
            "clarify": None}
    monkeypatch.setattr(mc, "generate_json", lambda *a, **k: (fake, True))
    monkeypatch.setattr(mc, "unload", lambda *a, **k: None)
    out = rule_wizard.draft("не принимать выписки для Германии", "bank")
    assert out["ok"] and out["rule"]["tier"] == "experimental"
    keys = [q["key"] for q in out["questions"]]
    assert keys == ["applies_to", "strictness", "countries"]
