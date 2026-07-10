"""Test runs live on their own tab.

A run whose document never came through the console is somebody exercising the
pipeline — the labeled corpus, the synthetic set, a CLI invocation — not the
operator doing masterdata work. Those runs are filed under Test runs and kept
out of the Documents list. The classification is derived from where the document
lives (save_upload is the only writer of inbox/), so nothing has to remember to
pass a flag; a stored flag overrides the derivation for good.
"""
import json

import pytest
from fastapi.testclient import TestClient

from mdmdoc import config, runstore


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    (tmp_path / "inbox").mkdir()
    return tmp_path


def _mk_run(root, rid: str, path, **meta) -> None:
    d = root / "runs" / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"path": str(path), "doc_class": "bank",
                                             "run_id": rid, "ts": "2026-07-10T10:00:00Z",
                                             **meta}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT", "doc_type": "bank_letter"}))


# ------------------------------------------------------------------ derivation
def test_uploaded_document_is_not_a_test_run(isolated):
    assert runstore.default_is_test(isolated / "inbox" / "abc__letter.pdf") is False


def test_anything_outside_the_inbox_is_a_test_run(isolated):
    assert runstore.default_is_test(isolated / "corpus" / "letter.pdf") is True
    assert runstore.default_is_test("/private/tmp/scratch.pdf") is True
    assert runstore.default_is_test("") is True          # unreadable meta -> test, not Documents


def test_stored_flag_beats_the_derivation(isolated):
    inbox_doc = isolated / "inbox" / "abc__letter.pdf"
    assert runstore.is_test({"path": str(inbox_doc)}) is False
    assert runstore.is_test({"path": str(inbox_doc), "test": True}) is True
    corpus_doc = isolated / "corpus" / "letter.pdf"
    assert runstore.is_test({"path": str(corpus_doc)}) is True
    assert runstore.is_test({"path": str(corpus_doc), "test": False}) is False


def test_runs_written_before_the_flag_existed_still_classify(isolated):
    """No migration touches operator data: legacy meta.json has no 'test' key."""
    _mk_run(isolated, "a" * 16, isolated / "inbox" / "a__doc.pdf")
    _mk_run(isolated, "b" * 16, isolated / "corpus" / "doc.pdf")
    assert [r["run_id"] for r in runstore.list_runs(test=False)] == ["a" * 16]
    assert [r["run_id"] for r in runstore.list_runs(test=True)] == ["b" * 16]
    assert len(runstore.list_runs()) == 2          # test=None -> everything


# ------------------------------------------------------------------------- UI
def _client(monkeypatch):
    monkeypatch.setenv("MDMDOC_MODE", "full")
    from mdmdoc.server.app import create_app
    return TestClient(create_app("full"))


def test_dashboard_hides_test_runs_and_the_tab_shows_them(isolated, monkeypatch):
    _mk_run(isolated, "a" * 16, isolated / "inbox" / "a__real.pdf")
    _mk_run(isolated, "b" * 16, isolated / "corpus" / "synthetic.pdf")
    c = _client(monkeypatch)

    docs = c.get("/ui").text
    assert "a__real.pdf" in docs and "synthetic.pdf" not in docs

    tests = c.get("/ui/test").text
    assert "synthetic.pdf" in tests and "a__real.pdf" not in tests


def test_move_endpoint_reclassifies_and_persists(isolated, monkeypatch):
    _mk_run(isolated, "b" * 16, isolated / "corpus" / "synthetic.pdf")
    c = _client(monkeypatch)
    assert runstore.is_test(runstore.load("b" * 16, "meta.json")) is True

    r = c.post(f"/api/v1/runs/{'b' * 16}/test", json={"test": False})
    assert r.status_code == 200 and r.json()["test"] is False
    assert runstore.is_test(runstore.load("b" * 16, "meta.json")) is False
    assert "synthetic.pdf" in c.get("/ui").text      # now in Documents, though outside inbox


def test_move_endpoint_validates(isolated, monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/v1/runs/deadbeefdeadbeef/test", json={"test": True}).status_code == 404
    _mk_run(isolated, "c" * 16, isolated / "corpus" / "x.pdf")
    assert c.post(f"/api/v1/runs/{'c' * 16}/test", json={}).status_code == 400
