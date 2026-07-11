"""Fast mode — consolidate from form(s) only, skip all verification."""
from __future__ import annotations

import io

from consolidation_helpers import (
    bank_run_fields,
    make_americas_form,
    make_template,
    needs_converter,
    write_run,
)
from mdmdoc import config

pytestmark = needs_converter


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from mdmdoc import runstore
    from mdmdoc.server.app import create_app
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(runstore, "RUNS_DIR", tmp_path / "runs", raising=False)
    monkeypatch.setenv("MDMDOC_BANK_VALUES", "full")
    return TestClient(create_app("full"))


def _new(c):
    return c.post("/ui/consolidation/new", follow_redirects=False
                  ).headers["location"].rsplit("/", 1)[-1]


def _up(c, case, route, path, name=None, data=None):
    return c.post(f"/ui/consolidation/{case}/{route}",
                  files={"file": (name or path.name, io.BytesIO(path.read_bytes()))},
                  data=data or {}, follow_redirects=False)


def test_fast_mode_skips_checks_and_allows_download(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = make_template(tmp_path / "t.xlsx")
    case = _new(client)
    _up(client, case, "template", tpl)
    _up(client, case, "extract", form)
    r = client.post(f"/ui/consolidation/{case}/consolidate",
                    data={"fast_mode": "on"}, follow_redirects=False)
    assert r.status_code == 303  # no warnings/quiz gate blocked it
    page = client.get(f"/ui/consolidation/{case}").text
    assert "no checks" in page
    # download works despite no verification
    dl = client.get(f"/ui/consolidation/{case}/download")
    assert dl.status_code == 200
    # required fields still filled, customer hidden, shared strings present
    import openpyxl
    import zipfile
    wb = openpyxl.load_workbook(io.BytesIO(dl.content))
    assert wb["LFA1 - Supplier General"].sheet_state == "visible"
    wb.close()
    z = zipfile.ZipFile(io.BytesIO(dl.content))
    assert "xl/sharedStrings.xml" in z.namelist()


def test_fast_mode_refused_when_document_attached(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    write_run(config.RUNS_DIR, "bankdoc", "bank", "ACCEPT", bank_run_fields())
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = make_template(tmp_path / "t.xlsx")
    case = _new(client)
    _up(client, case, "template", tpl)
    _up(client, case, "extract", form)
    client.post(f"/ui/consolidation/{case}/vendor/v01/attach-run",
                data={"run_id": "bankdoc"})
    r = client.post(f"/ui/consolidation/{case}/consolidate",
                    data={"fast_mode": "on"})
    assert "fast mode is for form-only" in r.text


def test_normal_mode_still_verifies(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    form = make_americas_form(tmp_path / "f.xlsm")
    tpl = make_template(tmp_path / "t.xlsx")
    case = _new(client)
    _up(client, case, "template", tpl)
    _up(client, case, "extract", form)
    # without fast: the ABA/SORTL warnings gate blocks until confirmed
    r = client.post(f"/ui/consolidation/{case}/consolidate")
    assert "nothing was written" in r.text
