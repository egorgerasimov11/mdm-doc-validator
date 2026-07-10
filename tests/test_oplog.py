"""E1/E2: the operator audit ledger — every mutating action logged via one
middleware seam, job lifecycle persisted, history/activity pages serve it."""
import time

import pytest

from mdmdoc import config, oplog


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    return tmp_path


def test_log_and_recent_filtering(env):
    oplog.log("mark-valid", run_id="r1", doc_class="bank")
    oplog.log("rule-approve", rule_id="BNK-001", doc_class="bank", detail="approved")
    oplog.log("job-start", job_id="j1", detail="check")
    oplog.log("job-end", job_id="j1", detail="check done 3s")
    rows = oplog.recent()
    assert [r["action"] for r in rows] == ["job-end", "job-start", "rule-approve",
                                           "mark-valid"]        # newest first
    jobs_only = oplog.recent(actions=("job-",))
    assert {r["action"] for r in jobs_only} == {"job-start", "job-end"}
    assert oplog.recent(actions=("rule-",))[0]["rule_id"] == "BNK-001"


def test_log_never_raises(env, monkeypatch):
    monkeypatch.setattr(config, "DATASET_DIR", tmp := env / "nope" / "\0bad")
    oplog.log("check", run_id="x")                       # must not raise


def test_middleware_logs_successful_posts(env, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    monkeypatch.setattr(config, "SETTINGS_PATH", env / "settings.json")
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/settings", json={"default_effort": 3})
    assert r.status_code == 200
    r = client.post("/api/v1/settings", json={"default_effort": 99})
    assert r.status_code == 400                          # failed → NOT logged
    rows = oplog.recent(actions=("settings",))
    assert len(rows) == 1


def test_job_lifecycle_persisted(env):
    from mdmdoc.server import jobs
    j = jobs.REGISTRY.submit("eval", lambda log: {"ok": True})
    end = None
    for _ in range(80):   # status flips before the finally-block writes the row
        rows = oplog.recent(actions=("job-",))
        end = next((r for r in rows
                    if r["action"] == "job-end" and r.get("job_id") == j.id), None)
        if end:
            break
        time.sleep(0.05)
    assert end is not None
    assert "eval done" in end["detail"]


def test_history_and_activity_pages(env, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    oplog.log("mark-valid", run_id="ab" * 8)
    client = TestClient(create_app("full"))
    r = client.get("/ui/history")
    assert r.status_code == 200 and "mark-valid" in r.text
    r = client.get("/ui/activity")
    assert r.status_code == 200 and "Activity" in r.text
