"""F1: every operator action in the History can be taken back — through the
same choke points, refusing when the state moved on, never twice."""
import json

import pytest
import yaml

from mdmdoc import challenges, config, dataset, oplog, patterns, rule_approvals, rules_io, undo

RULES_TEXT = """version: 1
doc_types: [bank_letter]
tables: {}
rules:
  - id: BNK-U01
    name: undo target
    tier: corp
    applies_to: [bank_letter]
    when: {always: true}
    severity: NOTE
    verdict_effect: null
    message: one
  - id: BNK-U02
    name: bystander
    applies_to: [bank_letter]
    when: {always: true}
    severity: NOTE
    verdict_effect: null
    message: two
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "FEWSHOT_DIR", tmp_path / "prompts" / "fewshot")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "banking.yaml").write_text(RULES_TEXT)
    return tmp_path


def _rule(doc_class="bank", rule_id="BNK-U01"):
    cfg = yaml.safe_load(rules_io.rules_text(doc_class))
    return next(r for r in cfg["rules"] if r["id"] == rule_id)


# ---------------------------------------------------------------- ledgers -----
def test_oplog_rows_carry_unique_ops(env):
    ops = {oplog.log("check", run_id="r1")["op"] for _ in range(30)}
    assert len(ops) == 30                      # ts alone would collide in-second


def test_challenges_retract_semantics(env):
    challenges.record("BNK-U01", "bank", "r1", "finding-downvote")
    challenges.record("BNK-U01", "bank", "r2", "valid-mark")
    challenges.retract("BNK-U01", "r1")
    assert challenges.counts() == {"BNK-U01": 1}
    live = challenges.for_rule("BNK-U01")
    assert [e["run_id"] for e in live] == ["r2"]      # r1's vote is gone
    challenges.retract("BNK-U01", "r2")
    assert challenges.counts() == {}


def test_labels_history_snapshot_and_restore(env):
    dataset.append_label({"doc_sha256": "aa" * 8, "ts": "T1", "doc_class": "bank",
                          "verdict_gold": "REJECT"})
    dataset.append_label({"doc_sha256": "aa" * 8, "ts": "T2", "doc_class": "bank",
                          "verdict_gold": "ACCEPT"})
    assert len(dataset.load_labels()) == 1            # latest wins, one row per sha
    prev = dataset.last_replaced_label("aa" * 8, replaced_ts="T2")
    assert prev and prev["ts"] == "T1" and "_replaced_ts" not in prev
    removed = dataset.delete_label("aa" * 8)
    assert removed["ts"] == "T2" and dataset.load_labels() == []


# ---------------------------------------------------------------- rules undo --
def test_undo_rule_delete_restores_pending(env):
    rule = _rule()
    rule_approvals.set_decision("bank", rule, rule_approvals.APPROVED)
    out = rules_io.delete_rule("bank", "BNK-U01")
    row = oplog.log("rule-delete", rule_id="BNK-U01", doc_class="bank",
                    detail=f"backup {out['backup'].rsplit('/', 1)[-1]}")
    res = undo.perform(row["op"])
    assert res["restored"] == "BNK-U01"
    assert "BNK-U01" in rules_io.rules_text("bank")
    store = rule_approvals.load()
    assert rule_approvals.status(store, "bank", _rule()) == rule_approvals.PENDING
    with pytest.raises(undo.StaleState):              # double undo refused
        undo.perform(row["op"])


def test_undo_rule_approve_reverts_decision(env):
    rule = _rule()
    rule_approvals.set_decision("bank", rule, "approved")
    row = oplog.log("rule-approve", rule_id="BNK-U01", doc_class="bank",
                    detail="pending -> approved")
    res = undo.perform(row["op"])
    assert res["decision"] == "pending"
    assert rule_approvals.status(rule_approvals.load(), "bank", rule) == "pending"


def test_undo_rule_approve_stale_when_redecided(env):
    rule = _rule()
    rule_approvals.set_decision("bank", rule, "approved")
    row = oplog.log("rule-approve", rule_id="BNK-U01", doc_class="bank",
                    detail="pending -> approved")
    rule_approvals.set_decision("bank", rule, "rejected")   # operator moved on
    with pytest.raises(undo.StaleState):
        undo.perform(row["op"])


def test_undo_rule_tier(env):
    rules_io.set_rule_tier("bank", "BNK-U01", "experimental")
    row = oplog.log("rule-tier", rule_id="BNK-U01", doc_class="bank",
                    detail="corp -> experimental")
    undo.perform(row["op"])
    assert _rule()["tier"] == "corp"


def test_undo_rule_save_restores_snapshot_and_refuses_after_newer_write(env):
    orig = rules_io.rules_text("bank")
    edited = orig.replace("message: one", "message: EDITED")
    rules_io.save_rules("bank", edited)               # snapshot taken inside
    row = oplog.log("rule-save", doc_class="bank")
    res = undo.perform(row["op"])
    assert "restored_snapshot" in res
    assert "EDITED" not in rules_io.rules_text("bank")
    # a second save AFTER a logged one blocks the older undo
    rules_io.save_rules("bank", edited)
    row2 = oplog.log("rule-save", doc_class="bank")
    rules_io.set_rule_tier("bank", "BNK-U02", "experimental")
    oplog.log("rule-tier", rule_id="BNK-U02", doc_class="bank",
              detail="? -> experimental")
    with pytest.raises(undo.StaleState):
        undo.perform(row2["op"])


def test_undo_rule_create_deletes_it(env):
    row = oplog.log("rule-create", rule_id="BNK-U02", doc_class="bank")
    res = undo.perform(row["op"])
    assert res["deleted"] == "BNK-U02"
    assert "BNK-U02" not in rules_io.rules_text("bank")


# ---------------------------------------------------------------- teach undo --
def test_undo_mark_valid_removes_label_pattern_challenges(env):
    sha = "fe" * 8
    dataset.append_label({"doc_sha256": sha, "ts": "T1", "doc_class": "bank",
                          "verdict_gold": "ACCEPT", "verdict_confirmed": True})
    patterns.record({"doc_sha256": sha, "ts": "T1", "doc_class": "bank",
                     "doc_type_gold": "bank_letter", "fields_gold": {},
                     "verdict_gold": "ACCEPT", "verdict_confirmed": True},
                    [], "NEED_MANUAL_REVIEW")
    challenges.record("BNK-U01", "bank", sha, "valid-mark")
    row = oplog.log("mark-valid", run_id=sha)
    res = undo.perform(row["op"])
    assert res["label_removed"] == sha
    assert dataset.load_labels() == []
    assert patterns.load() == []
    assert challenges.counts() == {}
    assert res["challenges_retracted"] == ["BNK-U01"]


def test_undo_label_restores_predecessor(env):
    sha = "ab" * 8
    dataset.append_label({"doc_sha256": sha, "ts": "T1", "doc_class": "bank",
                          "verdict_gold": "REJECT"})
    dataset.append_label({"doc_sha256": sha, "ts": "T2", "doc_class": "bank",
                          "verdict_gold": "ACCEPT"})
    row = oplog.log("label", run_id=sha)
    res = undo.perform(row["op"])
    labels = dataset.load_labels()
    assert len(labels) == 1 and labels[0]["ts"] == "T1"   # predecessor is back
    assert res["label_restored_ts"] == "T1"
    # the note must NOT claim "the precedent is gone" — a predecessor is back
    assert "gone" not in res["note"] and "still has an operator precedent" in res["note"]


def test_undo_note_flags_a_restored_unconfirmed_relaxation(env):
    # undoing a Mark valid that rolls back to an EARLIER unconfirmed-ACCEPT
    # correction must SAY so — that residual precedent is what keeps OPERATOR-2
    # firing, which is exactly what confused the operator.
    sha = "cd" * 8
    dataset.append_label({"doc_sha256": sha, "ts": "P1", "doc_class": "bank",
                          "verdict_gold": "ACCEPT", "verdict_confirmed": False})
    dataset.append_label({"doc_sha256": sha, "ts": "P2", "doc_class": "bank",
                          "verdict_gold": "ACCEPT", "verdict_confirmed": True})
    row = oplog.log("mark-valid", run_id=sha)
    res = undo.perform(row["op"])
    assert "OPERATOR-2" in res["note"] and "unconfirmed" in res["note"]


def test_undo_label_stale_after_newer_label(env):
    sha = "cd" * 8
    dataset.append_label({"doc_sha256": sha, "ts": "T1", "doc_class": "bank",
                          "verdict_gold": "ACCEPT"})
    row = oplog.log("mark-valid", run_id=sha)
    oplog.log("label", run_id=sha)                    # newer teach action
    with pytest.raises(undo.StaleState):
        undo.perform(row["op"])


def test_undo_rating_and_vote(env):
    from mdmdoc import ratings
    ratings.record("run1", "up")
    row = oplog.log("rating", run_id="run1", detail="up")
    undo.perform(row["op"])
    assert ratings.latest().get("run1", "") == ""
    challenges.record("BNK-U01", "bank", "run1", "finding-downvote")
    vrow = oplog.log("finding-vote", run_id="run1", rule_id="BNK-U01")
    undo.perform(vrow["op"])
    assert challenges.counts() == {}
    with pytest.raises(undo.StaleState):              # nothing left to retract
        undo.perform(oplog.log("finding-vote", run_id="run1", rule_id="BNK-U01")["op"])


# ---------------------------------------------------------------- surface -----
def test_not_undoable_actions_refuse(env):
    row = oplog.log("check", run_id="r1")
    with pytest.raises(undo.UndoError):
        undo.perform(row["op"])
    with pytest.raises(undo.UndoError):
        undo.perform("nonexistent")


def test_undo_endpoint_and_history_page(env, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rule = _rule()
    rule_approvals.set_decision("bank", rule, "approved")
    row = oplog.log("rule-approve", rule_id="BNK-U01", doc_class="bank",
                    detail="pending -> approved")
    client = TestClient(create_app("full"))
    html = client.get("/ui/history").text
    assert f'data-op="{row["op"]}"' in html           # Undo button offered
    r = client.post("/api/v1/undo", json={"op": row["op"]})
    assert r.status_code == 200 and r.json()["decision"] == "pending"
    r = client.post("/api/v1/undo", json={"op": row["op"]})
    assert r.status_code == 409                       # already undone
    html = client.get("/ui/history").text
    assert "line-through" in html                     # struck in the timeline
