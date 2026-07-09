import json
import os

import pytest
from fastapi.testclient import TestClient

from mdmdoc import config


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "dataset" / "mlx-lora")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "FEWSHOT_DIR", tmp_path / "prompts" / "fewshot")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    return tmp_path


def _client(mode: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("MDMDOC_MODE", mode)
    from mdmdoc.server.app import create_app
    return TestClient(create_app(mode))


def test_health_and_docs(isolated, monkeypatch):
    c = _client("full", monkeypatch)
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_only_hides_teach_and_ui(isolated, monkeypatch):
    c = _client("api-only", monkeypatch)
    paths = c.get("/openapi.json").json()["paths"]
    assert "/api/v1/check" in paths
    assert not any("train" in p or "label" in p or "eval" in p or "review" in p
                   for p in paths)
    assert c.get("/ui").status_code == 404


def test_doctor_degrades_gracefully(isolated, monkeypatch):
    from mdmdoc import model_client as mc
    monkeypatch.setattr(mc, "preflight",
                        lambda *a, **k: (_ for _ in ()).throw(mc.OllamaUnavailable("down")))
    c = _client("full", monkeypatch)
    r = c.get("/api/v1/doctor")
    assert r.status_code == 200
    assert r.json()["model_host"]["reachable"] is False


def test_token_auth(isolated, monkeypatch):
    monkeypatch.setenv("MDMDOC_API_TOKEN", "sekret")
    c = _client("api-only", monkeypatch)
    assert c.get("/api/v1/doctor").status_code == 401
    assert c.get("/api/v1/doctor",
                 headers={"Authorization": "Bearer sekret"}).status_code == 200


def test_token_via_cookie_and_query(isolated, monkeypatch):
    # <img>/download requests can't send the Bearer header — cookie + ?token= work
    monkeypatch.setenv("MDMDOC_API_TOKEN", "sekret")
    c = _client("full", monkeypatch)
    h = {"Host": "127.0.0.1"}
    assert c.get("/api/v1/doctor", headers=h).status_code == 401
    assert c.get("/api/v1/doctor", headers=h, cookies={"mdmdoc_token": "sekret"}).status_code == 200
    assert c.get("/api/v1/doctor?token=sekret", headers=h).status_code == 200
    assert c.get("/api/v1/doctor?token=nope", headers=h).status_code == 401


def test_ui_page_sets_token_cookie(isolated, monkeypatch):
    monkeypatch.setenv("MDMDOC_API_TOKEN", "sekret")
    c = _client("full", monkeypatch)
    r = c.get("/ui", headers={"Host": "127.0.0.1"})
    assert r.status_code == 200
    assert r.cookies.get("mdmdoc_token") == "sekret"


def test_host_header_hardening_full_mode(isolated, monkeypatch):
    c = _client("full", monkeypatch)
    r = c.get("/api/v1/runs", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_allowed_hosts_env_permits_tailnet(isolated, monkeypatch):
    # MDMDOC_ALLOWED_HOSTS lets the operator expose the console on a tailnet
    # name — but only WITH a token (C12): exposure without auth is refused
    monkeypatch.setenv("MDMDOC_ALLOWED_HOSTS", "omen.tail461272.ts.net, mini.example")
    monkeypatch.setenv("MDMDOC_API_TOKEN", "s3cret")
    c = _client("full", monkeypatch)
    auth = {"Authorization": "Bearer s3cret"}
    assert c.get("/api/v1/runs", headers={"Host": "omen.tail461272.ts.net:8766",
                                          **auth}).status_code == 200
    assert c.get("/api/v1/runs", headers={"Host": "mini.example", **auth}).status_code == 200
    assert c.get("/api/v1/runs", headers={"Host": "evil.example.com", **auth}).status_code == 403
    # allowed host but missing/wrong bearer -> 401
    assert c.get("/api/v1/runs", headers={"Host": "mini.example"}).status_code == 401
    assert c.get("/api/v1/runs", headers={"Host": "mini.example",
                                          "Authorization": "Bearer nope"}).status_code == 401


def test_allowed_hosts_without_token_refused(isolated, monkeypatch):
    # exposing the console without a token would leave the approval hard-gate
    # unauthenticated — every gated endpoint refuses, incl. Host: localhost
    # (the Host header is client-controlled, so no carve-out)
    monkeypatch.setenv("MDMDOC_ALLOWED_HOSTS", "mini.example")
    monkeypatch.delenv("MDMDOC_API_TOKEN", raising=False)
    c = _client("full", monkeypatch)
    assert c.get("/api/v1/runs", headers={"Host": "mini.example"}).status_code == 503
    assert c.get("/api/v1/runs", headers={"Host": "localhost"}).status_code == 503


def test_no_allowed_hosts_no_token_local_default_works(isolated, monkeypatch):
    monkeypatch.delenv("MDMDOC_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MDMDOC_API_TOKEN", raising=False)
    c = _client("full", monkeypatch)
    assert c.get("/api/v1/runs").status_code == 200          # localhost default


def test_check_async_job_lifecycle(isolated, monkeypatch):
    from mdmdoc.server import api as api_mod

    def fake_pipeline(path, doc_class, lang, use_vision, sap_image=None, quality=False,
                      web=False, engine="", sap_bp=""):
        return {"run_id": "deadbeef00000000", "verdict": "ACCEPT",
                "report": {"schema": "mdmdoc.v1"}, "report_md": "[BANK DOC VERDICT]"}

    monkeypatch.setattr(api_mod, "_run_pipeline", fake_pipeline)
    c = _client("full", monkeypatch)
    r = c.post("/api/v1/check", data={"doc_class": "bank", "wait": "false"},
               files={"file": ("x.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    import time
    for _ in range(50):
        j = c.get(f"/api/v1/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert j["status"] == "done" and j["result"]["verdict"] == "ACCEPT"
    # upload landed in inbox, content-addressed
    files = list(config.INBOX_DIR.iterdir())
    assert len(files) == 1 and files[0].name.endswith("__x.pdf")


def test_artifact_allowlist_blocks_traversal(isolated, monkeypatch):
    c = _client("full", monkeypatch)
    rid = "cafe0123cafe0123"
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"path": "/x", "doc_class": "bank"}))
    assert c.get(f"/api/v1/runs/{rid}/artifacts/meta.json").status_code == 200
    assert c.get(f"/api/v1/runs/{rid}/artifacts/..%2F..%2Fsecrets").status_code == 404
    assert c.get(f"/api/v1/runs/{rid}/artifacts/stage_b.json").status_code == 404


def test_label_over_http_masks(isolated, monkeypatch):
    rid = "beef4567beef4567"
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"path": "/x/doc.pdf", "file_name": "doc.pdf", "doc_class": "bank"}))
    (d / "extraction.json").write_text(json.dumps({
        "doc_class": "bank", "doc_type": "bank_letter",
        "fields": {"account_holder": "ACME", "bank_name": "DB", "bank_country": "DE",
                   "bank_address": "", "iban": {"present": False, "masked": ""},
                   "swift_bic": "", "account_number": {"present": False, "masked": ""},
                   "routing_aba": {"present": False, "masked": ""}, "currency": "",
                   "doc_date": "", "signed": True, "partial_capture": False}}))
    (d / "stage_a.json").write_text(json.dumps({"raw_text_excerpt": "",
                                                "regex_candidates_masked": {}}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))
    c = _client("full", monkeypatch)
    r = c.post(f"/api/v1/runs/{rid}/label", json={
        "fields": {"iban": {"action": "set", "value": "DE44500105175407324931"}},
        "doc_type_gold": "bank_letter", "verdict_gold": "ACCEPT"})
    assert r.status_code == 200
    blob = config.LABELS_PATH.read_text()
    assert "DE44500105175407324931" not in blob
    assert "DE**…4931" in blob
