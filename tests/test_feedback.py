"""Feedback -> proposed rule fix (rule_propose). The LLM call is mocked; the
deterministic parts (semantic validation, textual YAML splice, classification
routing) are the contract that must stay correct."""
import json

import yaml

from mdmdoc import config, rule_propose

GOOD = {"id": "BNK-099", "name": "x", "when": {"always": True},
        "severity": "WARNING", "verdict_effect": "WARNING",
        "message": "m", "message_ru": "м"}

SMALL = """version: 1
doc_types: [invoice, bank_letter]
rules:
  - id: A-1
    name: one
    when: {always: true}
    severity: NOTE
    verdict_effect: null
    message: "one"
  - id: A-2
    name: two
    when: {always: true}
    severity: WARNING
    verdict_effect: WARNING
    message: "two"
"""


# --- semantic validation (what save_rules does NOT check) --------------------
def test_validate_accepts_good():
    assert rule_propose.validate_rule(GOOD, "bank") == []


def test_validate_rejects_unknown_predicate():
    r = dict(GOOD, when={"check": "does_not_exist", "field": "iban"})
    assert any("does not exist" in i for i in rule_propose.validate_rule(r, "bank"))


def test_validate_rejects_bad_enums_and_operator():
    assert any("severity" in i for i in rule_propose.validate_rule(dict(GOOD, severity="LOUD"), "bank"))
    assert any("verdict_effect" in i for i in rule_propose.validate_rule(dict(GOOD, verdict_effect="MAYBE"), "bank"))
    assert any("operator" in i for i in rule_propose.validate_rule(dict(GOOD, when={"nope": 1}), "bank"))


def test_validate_blocks_document_value_in_message():
    # a rule message must never embed a document value (leaked account/IBAN)
    r = dict(GOOD, message="account 1830042757 is wrong")
    assert any("digit run" in i for i in rule_propose.validate_rule(r, "bank"))


# --- deterministic textual splice --------------------------------------------
def test_apply_edit_replaces_block_by_id():
    edited = dict(GOOD, id="A-1", name="one", message="changed")
    out = rule_propose.apply_change(SMALL, "edit", edited, "A-1")
    cfg = yaml.safe_load(out)
    a1 = next(r for r in cfg["rules"] if r["id"] == "A-1")
    assert a1["message"] == "changed"
    assert len(cfg["rules"]) == 2 and "A-2" in out          # other rule untouched
    assert "version: 1" in out and "doc_types:" in out       # header preserved


def test_apply_add_appends():
    out = rule_propose.apply_change(SMALL, "add", dict(GOOD, id="A-3"), "A-3")
    cfg = yaml.safe_load(out)
    assert [r["id"] for r in cfg["rules"]] == ["A-1", "A-2", "A-3"]


def test_apply_remove_deletes():
    out = rule_propose.apply_change(SMALL, "remove", {}, "A-1")
    cfg = yaml.safe_load(out)
    assert [r["id"] for r in cfg["rules"]] == ["A-2"]


# --- propose() classification (LLM mocked) -----------------------------------
def _mk_run(tmp, rid="cafe0123cafe0123", doc_class="bank"):
    d = tmp / "runs" / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"doc_class": doc_class, "run_id": rid, "path": "/x.pdf"}))
    (d / "findings.json").write_text(json.dumps(
        [{"rule_id": "BNK-020", "severity": "NOTE", "message": "old"}]))
    (d / "extraction.json").write_text(json.dumps(
        {"doc_type": "bank_letter", "fields": {"iban": {"masked": "…1234"}}}))
    (d / "report.json").write_text(json.dumps({"verdict": "WARNING"}))
    return rid


def _mock_llm(monkeypatch, obj):
    from mdmdoc import model_client as mc
    monkeypatch.setattr(mc, "generate_json", lambda *a, **k: (obj, True))
    monkeypatch.setattr(mc, "unload", lambda *a, **k: None)


def test_propose_rule_builds_valid_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    rid = _mk_run(tmp_path)
    _mock_llm(monkeypatch, {
        "kind": "rule", "action": "edit", "rule_id": "BNK-020",
        "rationale": "two years is too strict here",
        "rule": {"id": "BNK-020", "name": "doc_older_than_2y",
                 "when": {"check": "date_older_than", "field": "doc_date", "args": {"years": 3}},
                 "severity": "NOTE", "verdict_effect": None,
                 "message": "Document is older than 3 years ({detail})."}})
    out = rule_propose.propose(rid, "too strict on age")
    assert out["kind"] == "rule" and out["applicable"] is True
    assert "years: 3" in out["proposed_yaml"] and out["diff"]
    assert yaml.safe_load(out["proposed_yaml"])["rules"]      # still a valid file


def test_propose_routes_extraction_to_teach(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    rid = _mk_run(tmp_path)
    _mock_llm(monkeypatch, {"kind": "extraction", "rationale": "wrong account read"})
    out = rule_propose.propose(rid, "it read the wrong account number")
    assert out["kind"] == "extraction" and out["route"].endswith("/review")


def test_propose_flags_needs_code(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    rid = _mk_run(tmp_path)
    _mock_llm(monkeypatch, {"kind": "needs_code", "rationale": "no predicate for this",
                            "needs_code": "iban needs a numeric-value guard"})
    out = rule_propose.propose(rid, "iban rule needs a US guard")
    assert out["kind"] == "needs_code" and "numeric" in out["needs_code"]


def test_prompt_targets_disputed_rule():
    ctx = {"findings": [{"rule_id": "BNK-021", "severity": "WARNING", "message": "m"},
                        {"rule_id": "BNK-006", "severity": "NOTE", "message": "n"}],
           "pub": {"doc_type": "bank_letter", "fields": {}}, "report": {"verdict": "WARNING"}}
    p = rule_propose._prompt("dispute", ctx, "rules: []", rule_id="BNK-006")
    assert "DISPUTING RULE BNK-006" in p
    # the disputed rule is put first in the fired list so the model focuses on it
    assert p.index("rule_id: BNK-006") < p.index("rule_id: BNK-021")


# --- HTTP endpoint (job lifecycle) -------------------------------------------
def test_propose_fix_endpoint_runs_as_job(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from mdmdoc import rule_propose as rp, runstore
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("MDMDOC_MODE", "full")
    monkeypatch.setattr(runstore, "resolve_run", lambda r: r)             # skip the 404 check
    monkeypatch.setattr(rp, "propose", lambda run_id, fb, rid="": {"kind": "rule", "applicable": True,
                                                                   "proposed_yaml": "rules: []", "diff": "d"})
    from mdmdoc.server.app import create_app
    c = TestClient(create_app("full"))
    r = c.post("/api/v1/runs/cafe0123cafe0123/propose-fix", json={"feedback": "rule fired wrongly"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    for _ in range(50):
        j = c.get(f"/api/v1/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert j["status"] == "done" and j["result"]["kind"] == "rule"
    # empty feedback is rejected
    assert c.post("/api/v1/runs/cafe0123cafe0123/propose-fix", json={"feedback": " "}).status_code == 400
