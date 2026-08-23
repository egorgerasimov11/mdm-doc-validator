"""The extractor beta's UI mode: only Extract + Activity, dashboard redirects."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mdmdoc.server import extract_ui
from mdmdoc.server.app import create_app

RIB = {"file": "/x/RIB_ATREEC.pdf", "pages": 1, "pages_read": [0], "engines": ["tess:auto", "rapidocr:auto"],
       "doc_type": "RIB", "elapsed_s": 5.0, "transcript": "Banque 30003",
       "pages_out": [{"page": 0, "latency": {}, "primary_engine": "rapidocr:auto", "transcript": "Banque 30003",
                      "size": [1000, 1400], "values": [], "readings": {"tess:auto": "Banque 30003", "rapidocr:auto": "Banque 30003"},
                      "fields": [{"value": "30003", "pretty": "30003", "label": "Banque", "kind": "bank code", "group": "bank",
                                  "status": "confirmed", "voices": ["rapidocr:auto", "tess:auto"],
                                  "families": ["rapidocr", "tesseract"], "line": 0, "bbox_pct": [10, 10, 20, 12]}]}]}


@pytest.fixture()
def result(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_ui, "OUT_ROOT", tmp_path / "out")
    d = tmp_path / "out" / "RIB_ATREEC"
    d.mkdir(parents=True)
    (d / "extract.json").write_text(json.dumps(RIB), encoding="utf-8")
    (d / "extract.md").write_text("# RIB", encoding="utf-8")
    yield d
    shutil.rmtree(tmp_path / "out", ignore_errors=True)


def test_extract_mode_hides_the_validator(result, monkeypatch):
    monkeypatch.setenv("MDMDOC_UI_MODE", "extract")
    c = TestClient(create_app())
    r = c.get("/ui", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/extract"
    html = c.get("/ui/extract").text
    assert ">Extract<" in html and ">Activity" in html
    assert ">Documents<" not in html and ">Settings<" not in html and ">Data<" not in html
    act = c.get("/ui/activity").text
    assert "RIB_ATREEC.pdf" in act and '"section": "Extract"' in act
    assert 'data-v="doc"' not in act                      # no validator sections to filter by


def test_full_mode_keeps_the_console_and_adds_extractions_to_activity(result, monkeypatch):
    monkeypatch.setenv("MDMDOC_UI_MODE", "full")
    c = TestClient(create_app())
    assert c.get("/ui", follow_redirects=False).status_code == 200
    html = c.get("/ui/extract").text
    assert ">Documents<" in html and ">Settings<" in html
    act = c.get("/ui/activity").text
    assert "RIB_ATREEC.pdf" in act and 'data-v="doc"' in act


def test_result_page_and_page_image_routes(result, monkeypatch):
    monkeypatch.setenv("MDMDOC_UI_MODE", "extract")
    c = TestClient(create_app())
    html = c.get("/ui/extract/RIB_ATREEC").text
    assert "split-root" in html and "f-row" in html and 'data-bbox="10,10,20,12"' in html and "Banque" in html
    assert c.get("/ui/extract/RIB_ATREEC/page/0").status_code == 404      # source file is gone
    assert c.get("/ui/extract/../etc").status_code in (400, 404)
