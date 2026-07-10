"""E3-E6 (operator trust): the RULE-GATE names its rules with provenance and
offers actions; a rule can be PHYSICALLY deleted (backed up first); Mark valid
records challenges against every rule it overrides and re-runs the document;
a 👎 on one finding is remembered until the operator validates the rule."""
import json

import pytest
import yaml

from mdmdoc import challenges, config, oplog, rule_approvals, rules_io
from mdmdoc.rules.engine import run_rules

RULES = {
    "version": 1,
    "doc_types": ["bank_letter"],
    "tables": {},
    "rules": [
        {"id": "BNK-T01", "name": "always reject (test)", "tier": "experimental",
         "source": "skill:demo", "applies_to": ["bank_letter"],
         "when": {"always": True}, "severity": "CRITICAL",
         "verdict_effect": "REJECT", "message": "test reject"},
        {"id": "BNK-T02", "name": "always note (test)", "tier": "corp",
         "applies_to": ["bank_letter"], "when": {"always": True},
         "severity": "NOTE", "verdict_effect": None, "message": "test note"},
    ],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "FEWSHOT_DIR", tmp_path / "prompts" / "fewshot")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "banking.yaml").write_text(yaml.safe_dump(RULES, sort_keys=False))
    return tmp_path


def _ext():
    from mdmdoc.fields import Extraction
    return Extraction(doc_class="bank", doc_type="bank_letter")


def _mk_run(rid="feed1234feed1234", doc_class="bank", pending=None,
            findings=None, path="/nonexistent/doc.pdf"):
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True)
    meta = {"path": path, "file_name": "doc.pdf", "doc_class": doc_class,
            "run_id": rid, "ts": "2026-07-10T00:00:00Z"}
    if pending is not None:
        meta["pending_rules"] = pending
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "extraction.json").write_text(json.dumps(
        {"doc_class": doc_class, "doc_type": "bank_letter", "fields": {},
         "warnings": []}))
    (d / "stage_a.json").write_text(json.dumps({"has_text_layer": True}))
    (d / "findings.json").write_text(json.dumps(findings or []))
    (d / "report.json").write_text(json.dumps(
        {"verdict": "NEED_MANUAL_REVIEW", "doc_type": "bank_letter"}))
    return rid


# ---------------------------------------------------------------- challenges --
def test_challenges_ledger_counts_and_dismissal(env):
    challenges.record("BNK-T01", "bank", "r1", "valid-mark")
    challenges.record("BNK-T01", "bank", "r2", "finding-downvote")
    assert challenges.counts() == {"BNK-T01": 2}
    assert [e["kind"] for e in challenges.for_rule("BNK-T01")] == [
        "finding-downvote", "valid-mark"]                       # newest first
    challenges.dismiss_rule("BNK-T01", reason="rule stands")
    assert challenges.counts() == {}                            # live count reset
    challenges.record("BNK-T01", "bank", "r3", "valid-mark")
    assert challenges.counts() == {"BNK-T01": 1}                # fresh epoch
    assert len(challenges.for_rule("BNK-T01")) == 1             # boundary respected


# ---------------------------------------------------------------- E3 gate -----
def test_gate_finding_names_rules_with_provenance(env):
    pending: list = []
    findings = run_rules(_ext(), enforce_approvals=True, pending_out=pending)
    gate = next(f for f in findings if f.rule_id == "RULE-GATE")
    assert "BNK-T01 (skill:demo, experimental)" in gate.message
    assert {p["id"] for p in pending} == {"BNK-T01", "BNK-T02"}
    p1 = next(p for p in pending if p["id"] == "BNK-T01")
    assert p1["source"] == "skill:demo" and p1["tier"] == "experimental"
    assert p1["name"] == "always reject (test)"


def test_run_page_renders_gate_panel_not_gate_row(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(pending=[{"id": "BNK-T01", "name": "always reject (test)",
                            "source": "skill:demo", "tier": "experimental"}],
                  findings=[{"rule_id": "RULE-GATE", "severity": "WARNING",
                             "verdict_effect": "NEED_MANUAL_REVIEW",
                             "message": "1 rule(s) await your approval"}])
    html = TestClient(create_app("full")).get(f"/ui/runs/{rid}").text
    assert 'id="gate-panel"' in html
    assert "Blocked by unapproved rules" in html
    assert 'data-rule="BNK-T01"' in html and "skill:demo" in html
    # the RULE-GATE line is the panel now — not a dead row in Findings
    assert "await your approval" not in html.split('id="gate-panel"')[0]


# ---------------------------------------------------------------- E6 delete ---
def test_delete_rule_cuts_block_backs_up_and_clears_approval(env):
    cfg = yaml.safe_load(rules_io.rules_text("bank"))
    rule = next(r for r in cfg["rules"] if r["id"] == "BNK-T01")
    rule_approvals.set_decision("bank", rule, rule_approvals.APPROVED)
    out = rules_io.delete_rule("bank", "BNK-T01")
    assert out["remaining_rules"] == 1
    text = rules_io.rules_text("bank")
    assert "BNK-T01" not in text and "BNK-T02" in text
    yaml.safe_load(text)                                        # file still valid
    backups = list((config.RULES_DIR / "deleted").glob("BNK-T01-*.yaml"))
    assert len(backups) == 1 and "BNK-T01" in backups[0].read_text()
    # a future rule reusing the id starts PENDING — the old approval is gone
    store = rule_approvals.load()
    assert rule_approvals.status(store, "bank", rule) == rule_approvals.PENDING


def test_delete_rule_unknown_id_raises(env):
    with pytest.raises(ValueError):
        rules_io.delete_rule("bank", "BNK-NOPE")


def test_delete_rule_api_logs_and_dismisses_challenges(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    challenges.record("BNK-T01", "bank", "r1", "valid-mark")
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/rules/bank/delete", json={"rule_id": "BNK-T01"})
    assert r.status_code == 200 and r.json()["remaining_rules"] == 1
    assert challenges.counts() == {}                    # deletion resolves them
    rows = oplog.recent(actions=("rule-delete",))
    assert rows and rows[0]["rule_id"] == "BNK-T01"
    assert client.post("/api/v1/rules/bank/delete",
                       json={"rule_id": "BNK-T01"}).status_code == 400


def test_reject_dismisses_challenges(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    challenges.record("BNK-T01", "bank", "r1", "finding-downvote")
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/rules/bank/approve",
                    json={"rule_id": "BNK-T01", "decision": "rejected"})
    assert r.status_code == 200
    assert challenges.counts() == {}


# ---------------------------------------------------------------- E4 valid ----
def test_mark_valid_records_challenges_no_rerun_without_file(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(
        pending=[{"id": "BNK-T02", "name": "always note (test)",
                  "source": "", "tier": "corp"}],
        findings=[{"rule_id": "BNK-T01", "severity": "CRITICAL",
                   "verdict_effect": "REJECT", "message": "test reject"},
                  {"rule_id": "SAP-014", "severity": "WARNING",     # synthetic —
                   "verdict_effect": "NEED_MANUAL_REVIEW",          # never challenged
                   "message": "aborted"}])
    r = TestClient(create_app("full")).post(f"/api/v1/runs/{rid}/mark-valid")
    assert r.status_code == 200
    body = r.json()
    assert body["marked_valid"] is True
    ids = {c["id"] for c in body["challenged"]}
    assert ids == {"BNK-T01", "BNK-T02"}          # stricter finding + gate rule
    t01 = next(c for c in body["challenged"] if c["id"] == "BNK-T01")
    assert t01["source"] == "skill:demo" and t01["tier"] == "experimental"
    assert body["rerun_job_id"] == ""             # document file gone -> no job
    assert challenges.counts() == {"BNK-T01": 1, "BNK-T02": 1}


def test_mark_valid_spawns_rerun_when_document_exists(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server import api as api_mod
    from mdmdoc.server.app import create_app
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    rid = _mk_run(findings=[], path=str(doc))
    calls = {}

    def fake_pipeline(path, doc_class, lang, use_vision, *a, **kw):
        calls["path"] = str(path)
        return {"run_id": "rerun456rerun456", "verdict": "ACCEPT"}

    monkeypatch.setattr(api_mod, "_run_pipeline", fake_pipeline)
    r = TestClient(create_app("full")).post(f"/api/v1/runs/{rid}/mark-valid")
    assert r.status_code == 200
    job_id = r.json()["rerun_job_id"]
    assert job_id
    from mdmdoc.server import jobs
    for _ in range(100):
        j = jobs.REGISTRY.get(job_id)
        if j.status in ("done", "error"):
            break
        import time
        time.sleep(0.05)
    assert j.status == "done" and j.result["run_id"] == "rerun456rerun456"
    assert calls["path"] == str(doc)


# ---------------------------------------------------------------- E5 vote -----
def test_finding_vote_records_challenge_real_rules_only(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(findings=[{"rule_id": "BNK-T01", "severity": "CRITICAL",
                             "verdict_effect": "REJECT", "message": "test"}])
    client = TestClient(create_app("full"))
    r = client.post(f"/api/v1/runs/{rid}/findings/BNK-T01/vote", json={"vote": "down"})
    assert r.status_code == 200 and r.json()["challenges"] == 1
    r = client.post(f"/api/v1/runs/{rid}/findings/BNK-T01/vote", json={"vote": "down"})
    assert r.json()["challenges"] == 2
    rows = oplog.recent(actions=("finding-vote",))
    assert len(rows) == 2 and rows[0]["rule_id"] == "BNK-T01"
    # synthetic findings are not editable rules — nothing to challenge
    r = client.post(f"/api/v1/runs/{rid}/findings/SAP-014/vote", json={"vote": "down"})
    assert r.status_code == 400


def test_challenged_rules_surface_on_approvals_page(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    challenges.record("BNK-T01", "bank", "feed1234feed1234", "finding-downvote")
    challenges.record("BNK-T01", "bank", "feed1234feed1234", "valid-mark")
    html = TestClient(create_app("full")).get("/ui/rules/approve?doc_class=bank").text
    assert "Challenged rules" in html
    assert "⚠ challenged ×2" in html or "⚠ ×2" in html
    assert "skill:demo" in html                       # source is VISIBLE now


def test_dismiss_endpoint_clears_live_count(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    challenges.record("BNK-T01", "bank", "r1", "valid-mark")
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/rules/bank/challenges/dismiss",
                    json={"rule_id": "BNK-T01"})
    assert r.status_code == 200
    assert challenges.counts() == {}
    rows = oplog.recent(actions=("rule-challenge-dismiss",))
    assert rows and rows[0]["rule_id"] == "BNK-T01"


# ---------------------------------------------------------------- rule_stats --
def test_rule_stats_challenge_demotion_proposals(env):
    from mdmdoc import rule_stats
    base = {"doc_class": "bank", "verdict_effect": "REJECT", "fired": 3,
            "fired_confirmed": 0, "hits": 0, "precision": None,
            "wilson_lb": None, "age_days": None, "first_seen": ""}
    stats = [{**base, "rule_id": "BNK-T01", "tier": "corp", "challenges": 2},
             {**base, "rule_id": "BNK-T02", "tier": "experimental", "challenges": 3},
             {**base, "rule_id": "BNK-T03", "tier": "experimental", "challenges": 1}]
    props = {p["rule_id"]: p for p in rule_stats.propose(stats)}
    assert props["BNK-T01"]["to_tier"] == "experimental"        # corp steps down
    assert props["BNK-T02"]["to_tier"] == "reject-or-delete"
    assert "BNK-T03" not in props                               # below threshold
    assert props["BNK-T01"]["evidence"] == {"challenges": 2}


def test_rule_stats_compute_joins_live_challenge_counts(env):
    from mdmdoc import rule_stats
    challenges.record("BNK-T01", "bank", "r1", "valid-mark")
    stats = rule_stats.compute([], {"bank": RULES["rules"]})
    by_id = {s["rule_id"]: s for s in stats}
    assert by_id["BNK-T01"]["challenges"] == 1
    assert by_id["BNK-T02"]["challenges"] == 0


# ---------------------------------------------------------------- F2a ---------
def test_restore_deleted_rule_via_api(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/rules/bank/delete", json={"rule_id": "BNK-T01"})
    backup = r.json()["backup"].rsplit("/", 1)[-1]
    r = client.get("/api/v1/rules/deleted")
    assert r.status_code == 200
    assert any(d["backup"] == backup and d["doc_class"] == "bank" for d in r.json())
    r = client.post("/api/v1/rules/bank/restore", json={"backup": backup})
    assert r.status_code == 200 and r.json()["rule_id"] == "BNK-T01"
    assert "BNK-T01" in rules_io.rules_text("bank")
    # restored rule is PENDING (approval cleared at delete)
    cfg = yaml.safe_load(rules_io.rules_text("bank"))
    rule = next(x for x in cfg["rules"] if x["id"] == "BNK-T01")
    assert rule_approvals.status(rule_approvals.load(), "bank", rule) == "pending"
    rows = oplog.recent(actions=("rule-restore",))
    assert rows and rows[0]["rule_id"] == "BNK-T01"
    # approvals page offers the backup for OTHER deleted rules
    client.post("/api/v1/rules/bank/delete", json={"rule_id": "BNK-T02"})
    html = client.get("/ui/rules/approve?doc_class=bank").text
    assert "Deleted rules" in html and "rule-restore" in html


def test_inline_edit_block_roundtrip(env):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    client = TestClient(create_app("full"))
    # approve first so we can see the edit re-pend it
    cfg = yaml.safe_load(rules_io.rules_text("bank"))
    rule = next(x for x in cfg["rules"] if x["id"] == "BNK-T02")
    rule_approvals.set_decision("bank", rule, "approved")
    block = client.get("/api/v1/rules/bank/block/BNK-T02").text
    assert "BNK-T02" in block and "always note" in block
    edited = block.replace("test note", "edited note")
    r = client.post("/api/v1/rules/bank/edit",
                    json={"rule_id": "BNK-T02", "block": edited})
    assert r.status_code == 200
    text = rules_io.rules_text("bank")
    assert "edited note" in text and "BNK-T01" in text     # neighbor untouched
    cfg = yaml.safe_load(text)
    rule = next(x for x in cfg["rules"] if x["id"] == "BNK-T02")
    assert rule_approvals.status(rule_approvals.load(), "bank", rule) == "pending"
    # id change and broken yaml refused
    assert client.post("/api/v1/rules/bank/edit",
                       json={"rule_id": "BNK-T02",
                             "block": edited.replace("BNK-T02", "BNK-T99")}).status_code == 400
    assert client.post("/api/v1/rules/bank/edit",
                       json={"rule_id": "BNK-T02", "block": "not: [valid"}).status_code == 400
    assert client.get("/api/v1/rules/bank/block/BNK-NOPE").status_code == 404
