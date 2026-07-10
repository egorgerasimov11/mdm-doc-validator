"""V-wave: the Bulk API surface — job lifecycle, artifacts, downloads,
templates, cancelability of bulk jobs."""
import io
import time

from openpyxl import Workbook

from mdmdoc import config


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from mdmdoc import runstore
    from mdmdoc.server.app import create_app
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(runstore, "RUNS_DIR", tmp_path / "runs", raising=False)
    return TestClient(create_app("full"))


def _tax_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Business Partner", "Tax Number Category", "Tax Number",
               "Tax Number Long", "Country"])
    ws.append(["1", "US2", "12-3456780", "", "US"])
    ws.append(["2", "US0", "DE137196337", "", "US"])
    ws.append(["3", "US4", "XXXXXXX", "", "US"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _wait(client, jid, tries=100):
    for _ in range(tries):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] in ("done", "error", "canceled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_bulk_job_lifecycle_and_artifacts(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.post("/api/v1/bulk", data={"case": "auto"},
                    files={"file": ("t.xlsx", io.BytesIO(_tax_bytes()),
                                    "application/vnd.ms-excel")})
    assert r.status_code == 202
    j = _wait(client, r.json()["job_id"])
    assert j["status"] == "done", j.get("error")
    res = j["result"]
    assert res["case"] == "tax"
    assert res["summary"]["counts"]["INVALID"] == 1      # the DE VAT under US0
    assert res["summary"]["counts"]["SKIPPED"] == 1
    bid = res["bulk_id"]

    rep = client.get(f"/api/v1/runs/{bid}/artifacts/bulk_report.json")
    assert rep.status_code == 200
    body = rep.json()
    assert body["schema"] == "mdmdoc.bulk.v1"
    assert any("BULK-T03" in pr["rules"] for pr in body["problem_rows"])

    md = client.get(f"/api/v1/runs/{bid}/artifacts/bulk_report.md")
    assert md.status_code == 200 and "BULK VALIDATION" in md.text

    xls = client.get(f"/api/v1/bulk/{bid}/result")
    assert xls.status_code == 200 and len(xls.content) > 4000
    assert client.get("/api/v1/bulk/deadbeef00/result").status_code == 404


def test_bulk_rejects_bad_inputs(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.post("/api/v1/bulk", data={"case": "nope"},
                    files={"file": ("t.xlsx", b"x", "application/vnd.ms-excel")})
    assert r.status_code == 400
    r = client.post("/api/v1/bulk", data={"case": "auto"},
                    files={"file": ("t.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400


def test_templates_served(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    for case in ("bank", "tax", "region"):
        r = client.get(f"/api/v1/bulk/templates/{case}")
        assert r.status_code == 200 and len(r.content) > 4000, case
    assert client.get("/api/v1/bulk/templates/nope").status_code == 404


def test_bulk_jobs_are_cancelable_kind(monkeypatch, tmp_path):
    from mdmdoc.server import jobs
    j = jobs.Job(id="x1", kind="bulk")
    assert j.to_dict()["cancelable"] is True
    j2 = jobs.Job(id="x2", kind="eval")
    assert j2.to_dict()["cancelable"] is False


def test_ui_bulk_page(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.get("/ui/bulk")
    assert r.status_code == 200 and "Bulk validation" in r.text
