"""F4: the operator teaches the document TYPE from a dropdown — a light label
(type only, NO verdict opinion), a background re-run, full undo."""
import json

import pytest

from mdmdoc import config, dataset, oplog, undo


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
    (tmp_path / "rules" / "banking.yaml").write_text(
        "version: 1\ndoc_types: [bank_letter, invoice]\ntables: {}\nrules: []\n")
    return tmp_path


def _mk_run(tmp_path, rid="beef1234beef1234", doc_type="invoice", path=None):
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"path": path or "/nonexistent/doc.pdf", "file_name": "doc.pdf",
         "doc_class": "bank", "run_id": rid, "ts": "2026-07-10T00:00:00Z"}))
    (d / "extraction.json").write_text(json.dumps(
        {"doc_class": "bank", "doc_type": doc_type, "fields": {}, "warnings": []}))
    (d / "stage_a.json").write_text(json.dumps({"has_text_layer": True}))
    (d / "findings.json").write_text("[]")
    (d / "report.json").write_text(json.dumps(
        {"verdict": "REJECT", "doc_type": doc_type}))
    return rid


def test_teach_type_writes_type_only_label(env, tmp_path):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(tmp_path)
    client = TestClient(create_app("full"))
    r = client.post(f"/api/v1/runs/{rid}/teach-type",
                    json={"doc_type": "bank_letter"})
    assert r.status_code == 200
    body = r.json()
    assert body["taught"] == "bank_letter"
    assert body["rerun_job_id"] == ""            # original file gone -> no job
    labels = dataset.load_labels()
    assert len(labels) == 1
    lab = labels[0]
    assert lab["doc_type_gold"] == "bank_letter"
    assert lab["verdict_gold"] == ""             # NO verdict opinion — the old
    assert lab["confirmed"] is True              # machine verdict must not pin
    rows = oplog.recent(actions=("teach-type",))
    assert rows and "bank_letter" in rows[0]["detail"]


def test_teach_type_precedent_flips_type_not_verdict(env, tmp_path):
    """The precedent from a teach-only label overrides doc_type on the next
    run and leaves the verdict to the LIVE machine (empty gold falls back)."""
    from mdmdoc.pipeline import _find_precedent
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(tmp_path)
    TestClient(create_app("full")).post(f"/api/v1/runs/{rid}/teach-type",
                                        json={"doc_type": "bank_letter"})
    prec = _find_precedent(rid)
    assert prec is not None
    assert prec["doc_type_gold"] == "bank_letter"
    assert prec["verdict_gold"] == ""
    assert not prec.get("verdict_confirmed")


def test_teach_type_rejects_unknown_type_and_missing_run(env, tmp_path):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(tmp_path)
    client = TestClient(create_app("full"))
    assert client.post(f"/api/v1/runs/{rid}/teach-type",
                       json={"doc_type": "w9"}).status_code == 400
    assert client.post("/api/v1/runs/nonexistent0000/teach-type",
                       json={"doc_type": "bank_letter"}).status_code == 404


def test_teach_type_spawns_rerun_when_file_exists(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server import api as api_mod
    from mdmdoc.server.app import create_app
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    rid = _mk_run(tmp_path, path=str(doc))
    monkeypatch.setattr(api_mod, "_run_pipeline",
                        lambda *a, **kw: {"run_id": rid, "verdict": "ACCEPT"})
    r = TestClient(create_app("full")).post(f"/api/v1/runs/{rid}/teach-type",
                                            json={"doc_type": "bank_letter"})
    job_id = r.json()["rerun_job_id"]
    assert job_id
    import time

    from mdmdoc.server import jobs
    for _ in range(100):
        if jobs.REGISTRY.get(job_id).status in ("done", "error"):
            break
        time.sleep(0.05)
    assert jobs.REGISTRY.get(job_id).status == "done"


def test_teach_type_undo_removes_label(env, tmp_path):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(tmp_path)
    TestClient(create_app("full")).post(f"/api/v1/runs/{rid}/teach-type",
                                        json={"doc_type": "bank_letter"})
    row = oplog.recent(actions=("teach-type",))[0]
    res = undo.perform(row["op"])
    assert res["label_removed"] == rid
    assert dataset.load_labels() == []


def test_run_page_renders_type_dropdown(env, tmp_path):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    rid = _mk_run(tmp_path)
    html = TestClient(create_app("full")).get(f"/ui/runs/{rid}").text
    assert 'id="type-select"' in html and 'id="btn-teach-type"' in html
    assert '<option value="invoice" selected>' in html
    assert '<option value="bank_letter" ' in html


def test_eval_skips_verdict_for_teach_only_labels(env):
    """A verdict-goldless label scores the TYPE and leaves the verdict metrics
    untouched (denominator shrinks — no invented opinion)."""
    from mdmdoc.evalrun import verdict_metrics
    pairs = [("ACCEPT", "ACCEPT"), ("REJECT", "ACCEPT")]
    m_all = verdict_metrics(pairs)
    assert m_all["verdict_accuracy"] == 0.5
    # the runner filters goldless pairs OUT before verdict_metrics — simulate
    filtered = [p for p in pairs + [("ACCEPT", "")] if p[1]]
    assert verdict_metrics(filtered)["verdict_accuracy"] == 0.5
