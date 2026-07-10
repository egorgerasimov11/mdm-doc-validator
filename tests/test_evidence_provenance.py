"""Tests for evidence crops (zone resolution + endpoint, never persisted) and
per-field provenance ({source, page} in extraction.json)."""
import json

import fitz
import pytest
from fastapi.testclient import TestClient

from mdmdoc import config
from mdmdoc.fields import Extraction, crosscheck_ids
from mdmdoc.rules.engine import run_rules
from mdmdoc.stage_a import RawDoc
from mdmdoc.stage_b import (_resolve_signature, _apply_w9_zone_probe,
                            _attribute_page, _finalize_provenance)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "dataset" / "mlx-lora")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    return tmp_path


def _bank_raw(**kw) -> RawDoc:
    d = dict(path="/x/doc.pdf", sha256="ab" * 32, ext=".pdf", doc_class="bank")
    d.update(kw)
    return RawDoc(**d)


# ---------------------------------------------------------------- findings ----
def test_finding_carries_field_name():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"account_holder": "", "bank_name": "DB", "signed": False,
                "signature_evidence": "", "partial_capture": False,
                "iban": "", "account_number": ""}
    findings = {f.rule_id: f for f in run_rules(e)}
    assert findings["BNK-023"].field == "account_holder"
    assert findings["BNK-021"].field == "signed"       # bound in banking.yaml
    assert findings["BNK-024"].field == ""             # no_bank_ids: not field-bound
    assert "field" in findings["BNK-023"].to_dict()


# ---------------------------------------------------------------- provenance --
def test_crosscheck_records_provenance():
    fields = {"account_number": "", "routing_aba": "211070175", "iban": ""}
    det = {"account_number": "1408137817", "routing_aba": "211070175"}
    prov: dict = {}
    notes = crosscheck_ids(fields, det, "bank", prov=prov)
    assert fields["account_number"] == "1408137817"
    assert prov["account_number"]["source"] == "ocr-regex"
    assert prov["routing_aba"] == {"source": "model", "page": None, "confirmed": True}
    assert any("routing_aba=confirmed" in n for n in notes)


def test_zone_probe_and_signature_probe_provenance():
    raw = _bank_raw(doc_class="w9", pages_used=[0],
                    w9_probe={"classification": "S corporation", "llc_code": "",
                              "tin_type": "EIN", "tin_digits": "810826734", "page": 0},
                    signature_probe={"handwritten_signature": True, "stamp": False,
                                     "evidence": "ink strokes", "page": 0})
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"line3_classification": "Individual", "tin_raw": "", "tin_type": "",
                "signed": False}
    _apply_w9_zone_probe(e, raw)
    _resolve_signature(e, raw)
    assert e.fields["line3_classification"] == "S corporation"
    assert e.provenance["line3_classification"] == {"source": "zone-probe", "page": 1}
    assert e.provenance["tin_raw"] == {"source": "zone-probe", "page": 1}
    assert e.fields["signed"] is True
    assert e.provenance["signed"] == {"source": "vision-crop", "page": 1}


def test_finalize_provenance_defaults_to_model_with_page():
    raw = _bank_raw(pages_used=[2, 3],
                    page_texts={2: "Bank of America, N.A maintains the account",
                                3: "Invoice number 42"})
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"bank_name": "Bank of America, N.A", "account_holder": "",
                "signed": True}
    e.provenance["signed"] = {"source": "vision-crop", "page": 1}
    _finalize_provenance(e, raw)
    assert e.provenance["bank_name"] == {"source": "model", "page": 3}
    assert "account_holder" not in e.provenance          # empty fields carry none
    assert e.provenance["signed"]["source"] == "vision-crop"  # special source kept
    assert e.provenance["doc_type"] == {"source": "model", "page": None}


def test_attribute_page_digit_normalized_and_single_page():
    raw = _bank_raw(pages_used=[1, 4],
                    page_texts={1: "nothing here", 4: "Account No: 1830 042 757"})
    assert _attribute_page(raw, "1830042757") == 5
    assert _attribute_page(raw, True) is None
    single = _bank_raw(pages_used=[0], page_texts={0: "whatever"})
    assert _attribute_page(single, "not-even-in-text") == 1


def test_to_public_folds_tin_provenance():
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"tin_raw": "81-0826734", "tin_type": "EIN", "line1_name": "ACME"}
    e.provenance = {"tin_raw": {"source": "zone-probe", "page": 1},
                    "tin_type": {"source": "zone-probe", "page": 1},
                    "line1_name": {"source": "model", "page": 1}}
    pub = e.to_public()
    assert pub["provenance"]["tin"] == {"source": "zone-probe", "page": 1}
    assert "tin_raw" not in pub["provenance"] and "tin_type" not in pub["provenance"]
    assert pub["provenance"]["line1_name"]["source"] == "model"
    # provenance carries no sensitive values
    from mdmdoc.privacy import assert_no_leak
    assert assert_no_leak(json.dumps(pub["provenance"]), ["81-0826734"]) == []


# ---------------------------------------------------------------- evidence ----
def _make_bank_pdf(path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Please accept this letter as confirmation of account.")
    page.insert_text((72, 200), "Routing Number: 123456789")
    page.insert_text((72, 230), "Account Number: 987654321")
    page.insert_text((72, 700), "Sincerely,")
    doc.save(str(path))
    doc.close()


def _make_run(tmp_path, rid: str, doc_class: str, doc_type: str, fields: dict,
              pdf_name: str = "doc.pdf") -> str:
    pdf = tmp_path / pdf_name
    if not pdf.exists():
        _make_bank_pdf(pdf)
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(
        {"path": str(pdf), "file_name": pdf.name, "doc_class": doc_class,
         "run_id": rid}))
    (d / "stage_a.json").write_text(json.dumps(
        {"has_text_layer": True, "pages_used": [0], "pages": 1, "rotations": {}}))
    (d / "extraction.json").write_text(json.dumps(
        {"doc_class": doc_class, "doc_type": doc_type, "fields": fields,
         "provenance": {}}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))
    return rid


BANK_FIELDS = {
    "account_number": {"present": True, "masked": "…4321", "value": "987654321"},
    "routing_aba": {"present": True, "masked": "…6789", "value": "123456789"},
    "iban": "", "swift_bic": "", "signed": False,
}


def test_resolve_and_render_bank_evidence_never_persists(isolated, tmp_path):
    from mdmdoc import evidence
    rid = _make_run(tmp_path, "feed0123feed0123", "bank", "bank_letter", BANK_FIELDS)
    specs = evidence.resolve_all(rid)
    assert {"account_number", "routing_aba", "signature"} <= set(specs)
    assert "iban" not in specs and "w9_class" not in specs
    for s in specs.values():
        assert s["page"] == 0
        x0, y0, x1, y1 = s["zone"]
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    # the routing line sits above the account line on the page
    assert specs["routing_aba"]["zone"][1] < specs["account_number"]["zone"][1]

    run_dir = config.RUNS_DIR / rid
    before = sorted(p.name for p in run_dir.rglob("*"))
    png = evidence.render_crop(rid, "account_number")
    assert png and png[:4] == b"\x89PNG"
    assert sorted(p.name for p in run_dir.rglob("*")) == before   # nothing persisted
    assert evidence.render_crop(rid, "w9_tin") is None


def test_resolve_w9_zone_evidence(isolated, tmp_path):
    from mdmdoc import evidence
    from mdmdoc.stage_a import W9_CLASS_ZONE, W9_TIN_ZONE
    rid = _make_run(tmp_path, "beefbeef00001111", "w9", "w9",
                    {"line1_name": "ACME", "signed": True}, pdf_name="w9.pdf")
    specs = evidence.resolve_all(rid)
    assert specs["w9_class"]["zone"] == W9_CLASS_ZONE
    assert specs["w9_tin"]["zone"] == W9_TIN_ZONE
    assert "signature" in specs
    # a W-8 must never get W-9 zones
    rid2 = _make_run(tmp_path, "beefbeef00002222", "w9", "w8",
                     {"line1_name": "ACME"}, pdf_name="w9.pdf")
    specs2 = evidence.resolve_all(rid2)
    assert "w9_class" not in specs2 and "w9_tin" not in specs2


def _client(mode: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("MDMDOC_MODE", mode)
    from mdmdoc.server.app import create_app
    return TestClient(create_app(mode))


def test_evidence_endpoint_streams_no_store(isolated, tmp_path, monkeypatch):
    rid = _make_run(tmp_path, "cafe4321cafe4321", "bank", "bank_letter", BANK_FIELDS)
    c = _client("full", monkeypatch)
    r = c.get(f"/api/v1/runs/{rid}/evidence/routing_aba")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert c.get(f"/api/v1/runs/{rid}/evidence/nonsense").status_code == 404
    assert c.get(f"/api/v1/runs/{rid}/evidence/w9_tin").status_code == 404


def test_evidence_endpoint_absent_in_api_only(isolated, monkeypatch):
    c = _client("api-only", monkeypatch)
    paths = c.get("/openapi.json").json()["paths"]
    assert not any("evidence" in p for p in paths)
