"""F5: the doc-type pattern memory — fills ONLY the weak fallback, never
overrides a valid answer, scales with effort, learns only from trusted
documents, stays out of eval."""
import json

import pytest

from mdmdoc import config, doctype_profiles as dp
from mdmdoc.fields import Extraction


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    dp._EMB_CACHE.clear()
    return tmp_path


class FakeRaw:
    def __init__(self, **kw):
        self.sha256 = kw.pop("sha256", "f" * 16)
        self.editable = kw.pop("editable", False)
        self.ext = kw.pop("ext", ".pdf")
        self.type_hint = kw.pop("type_hint", "")
        self.bank_letter_pages = kw.pop("bank_letter_pages", [])
        self.invoice_pages = kw.pop("invoice_pages", [])
        self.w9_pages = kw.pop("w9_pages", [])
        self.page_texts = kw.pop("page_texts", {})
        self.survey_texts = kw.pop("survey_texts", {})
        self.raw_text = kw.pop("raw_text", "")
        self.images = kw.pop("images", [])


def _profile(doc_type="bank_letter", run_id="r1", emb=None, markers=None,
             emb_kind="text", source="taught", doc_class="bank"):
    return {"ts": "T", "run_id": run_id, "doc_class": doc_class,
            "doc_type": doc_type, "markers": markers or
            {"bank_letter": True, "invoice": False, "w9_form": False,
             "type_hint": ""},
            "emb": emb if emb is not None else [], "emb_kind": emb_kind,
            "dims": len(emb or []), "source": source}


def _write(rows, tmp_path):
    (tmp_path / "dataset").mkdir(exist_ok=True)
    (tmp_path / "dataset" / "doctype_profiles.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")


def _ext(doc_type="other", doc_class="bank"):
    e = Extraction(doc_class=doc_class, doc_type=doc_type)
    e.provenance["doc_type"] = {"source": "model", "page": None}
    return e


# ---------------------------------------------------------------- matching ----
def test_markers_vote_needs_three_unanimous_docs(env, tmp_path):
    raw = FakeRaw(bank_letter_pages=[0])
    rows = [_profile(run_id=f"r{i}") for i in range(2)]
    assert not dp.markers_vote(rows, raw).decisive          # 2 docs — not enough
    rows.append(_profile(run_id="r3"))
    v = dp.markers_vote(rows, raw)
    assert v.decisive and v.doc_type == "bank_letter"
    # one same-signature profile of ANOTHER type kills the vote
    rows.append(_profile(doc_type="bank_statement", run_id="r4"))
    assert not dp.markers_vote(rows, raw).decisive


def test_embed_match_thresholds(env, tmp_path, monkeypatch):
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[1.0, 0.0]])
    raw = FakeRaw(page_texts={0: "bank confirmation letter text"})
    rows = [_profile(run_id="r1", emb=[1.0, 0.0]),
            _profile(run_id="r2", emb=[0.99, 0.14])]
    t, why = dp.embed_match(rows, raw)
    assert t == "bank_letter" and "2 taught" in why
    # single supporting doc -> refused
    t, _ = dp.embed_match([_profile(run_id="r1", emb=[1.0, 0.0])], raw)
    assert t == ""
    # a close competitor of another type kills the margin
    rows.append(_profile(doc_type="bank_statement", run_id="r3", emb=[0.999, 0.02]))
    t, _ = dp.embed_match(rows, raw)
    assert t == ""


def test_embed_cache_survives_ladder_rerun(env, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dp.mc, "embed",
                        lambda texts: calls.append(1) or [[1.0, 0.0]])
    rows = [_profile(run_id="r1", emb=[1.0, 0.0]),
            _profile(run_id="r2", emb=[1.0, 0.0])]
    raw = FakeRaw(page_texts={0: "same text"})
    dp.embed_match(rows, raw)
    dp.embed_match(rows, raw)                     # ladder re-run, same page-1
    assert len(calls) == 1                        # cached
    raw.page_texts = {0: "ENRICHED text"}         # ladder enriched page 1
    dp.embed_match(rows, raw)
    assert len(calls) == 2                        # re-embedded


# ---------------------------------------------------------------- the prior ---
def test_prior_fills_only_weak_fallback(env, tmp_path):
    _write([_profile(run_id=f"r{i}") for i in range(3)], tmp_path)
    raw = FakeRaw(bank_letter_pages=[0])
    e = _ext("other")
    dp.apply_prior(e, raw, ("bank_letter", "other"), weak_fallback=True,
                   engine="deterministic", quality=False)
    assert e.doc_type == "bank_letter"
    assert e.provenance["doc_type"]["source"] == "pattern"
    assert any("doc-type prior" in w for w in e.warnings)


def test_prior_never_overrides_valid_model_value(env, tmp_path):
    _write([_profile(run_id=f"r{i}") for i in range(3)], tmp_path)
    raw = FakeRaw(bank_letter_pages=[0])
    e = _ext("bank_statement")                    # a VALID model answer
    dp.apply_prior(e, raw, ("bank_letter", "bank_statement"), weak_fallback=False,
                   engine="deterministic", quality=False)
    assert e.doc_type == "bank_statement"         # untouched
    assert e.doc_type_uncertain is True           # but flagged
    assert any("disagrees" in w for w in e.warnings)


def test_prior_respects_rule_provenance_and_fences(env, tmp_path):
    _write([_profile(run_id=f"r{i}") for i in range(3)], tmp_path)
    raw = FakeRaw(bank_letter_pages=[0])
    e = _ext("other")
    e.provenance["doc_type"] = {"source": "rule", "page": None}
    dp.apply_prior(e, raw, ("bank_letter", "other"), weak_fallback=True,
                   engine="deterministic", quality=False)
    assert e.doc_type == "other"                  # rule provenance wins
    e = _ext("other")
    raw_inv = FakeRaw(invoice_pages=[0])          # invoice fence
    dp.apply_prior(e, raw_inv, ("bank_letter", "other"), weak_fallback=True,
                   engine="deterministic", quality=False)
    assert e.doc_type == "other"


def test_prior_deterministic_never_calls_models(env, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("model call on the deterministic path")
    monkeypatch.setattr(dp.mc, "embed", boom)
    monkeypatch.setattr(dp.mc, "vision", boom)
    # profiles exist but markers do NOT match -> no vote, and NO fallback to
    # the model tiers on the deterministic engine
    _write([_profile(run_id=f"r{i}") for i in range(3)], tmp_path)
    e = _ext("other")
    dp.apply_prior(e, FakeRaw(), ("bank_letter", "other"), weak_fallback=True,
                   engine="deterministic", quality=False)
    assert e.doc_type == "other"


def test_prior_eval_kill_switch(env, tmp_path):
    _write([_profile(run_id=f"r{i}") for i in range(3)], tmp_path)
    from mdmdoc import runctl
    ctl = runctl.RunControl(overrides={"doctype_prior": False})
    token = runctl.CURRENT.set(ctl)
    try:
        e = _ext("other")
        dp.apply_prior(e, FakeRaw(bank_letter_pages=[0]),
                       ("bank_letter", "other"), weak_fallback=True,
                       engine="deterministic", quality=False)
        assert e.doc_type == "other"              # gated off for eval
    finally:
        runctl.CURRENT.reset(token)


def test_prior_empty_ledger_total_noop(env, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not be called")
    monkeypatch.setattr(dp.mc, "embed", boom)
    e = _ext("other")
    dp.apply_prior(e, FakeRaw(bank_letter_pages=[0]), ("bank_letter", "other"),
                   weak_fallback=True, engine="auto", quality=True)
    assert e.doc_type == "other" and not e.warnings


# ---------------------------------------------------------------- capture -----
def _mk_run(tmp_path, rid, doc_type="bank_letter", labeled_type="", pdf=True):
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{rid}.pdf"
    if pdf:
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Bank confirmation letter for account holder")
        doc.save(path)
        doc.close()
    (d / "meta.json").write_text(json.dumps(
        {"path": str(path), "file_name": path.name, "doc_class": "bank",
         "run_id": rid, "ts": "2026-07-10T00:00:00Z", "test": False}))
    (d / "stage_a.json").write_text(json.dumps(
        {"bank_letter_pages": [0], "invoice_pages": [], "w9_pages": [],
         "type_hint": ""}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT",
                                               "doc_type": doc_type}))
    if labeled_type:
        (tmp_path / "dataset").mkdir(exist_ok=True)
        with open(config.LABELS_PATH, "a") as f:
            f.write(json.dumps({"doc_sha256": rid, "doc_type_gold": labeled_type,
                                "confirmed": True, "doc_class": "bank"}) + "\n")
    return rid


def test_capture_idempotent_and_closed_set(env, tmp_path, monkeypatch):
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[0.5, 0.5]])
    rid = _mk_run(tmp_path, "aaaa000000000001")
    row = dp.capture(rid, "taught", doc_type="bank_letter")
    assert row and row["emb"] == [0.5, 0.5] and row["markers"]["bank_letter"]
    assert dp.capture(rid, "taught", doc_type="bank_letter") is None   # dedup
    assert len(dp.load()) == 1
    # closed set: unknown/other teaches nothing
    rid2 = _mk_run(tmp_path, "aaaa000000000002")
    assert dp.capture(rid2, "taught", doc_type="martian_form") is None
    assert dp.capture(rid2, "taught", doc_type="other") is None
    # embed down -> markers-only profile still lands
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[]])
    assert dp.capture(rid2, "taught", doc_type="bank_letter")["emb"] == []


def test_drop_by_source_and_all(env, tmp_path, monkeypatch):
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[]])
    rid = _mk_run(tmp_path, "aaaa000000000003")
    dp.capture(rid, "taught", doc_type="bank_letter")
    dp.capture(rid, "rated-up", doc_type="bank_letter")
    assert dp.drop(rid, source="rated-up") == 1
    assert len(dp.load()) == 1
    assert dp.drop(rid) == 1
    assert dp.load() == []


# ---------------------------------------------------------------- study -------
def test_study_learns_only_trusted_docs(env, tmp_path, monkeypatch):
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[1.0]])
    _mk_run(tmp_path, "aaaa000000000010", labeled_type="bank_letter")   # labeled
    _mk_run(tmp_path, "aaaa000000000011")                               # bare
    _mk_run(tmp_path, "aaaa000000000012")                               # rated up
    from mdmdoc import ratings
    ratings.record("aaaa000000000012", "up")
    logs = []
    rep = dp.study(log=logs.append)
    assert rep["added"] == 2                     # labeled + rated-up, not bare
    assert rep["skipped"] == 1
    assert rep["by_type"] == {"bank_letter": 2}
    assert (config.EVAL_DIR / "pattern_study.json").exists()


def test_study_cancel_stops_early(env, tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[1.0]])
    for i in range(5):
        _mk_run(tmp_path, f"aaaa00000000002{i}", labeled_type="bank_letter")
    ev = threading.Event()
    ev.set()                                     # canceled before the first doc
    rep = dp.study(log=lambda *_: None, cancel=ev)
    assert rep["added"] == 0


def test_study_endpoint_spawns_cancelable_job(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(dp.mc, "embed", lambda texts: [[1.0]])
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/train/pattern-study", json={})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    import time

    from mdmdoc.server import jobs
    for _ in range(100):
        j = jobs.REGISTRY.get(job_id)
        if j.status in ("done", "error"):
            break
        time.sleep(0.05)
    assert jobs.REGISTRY.get(job_id).status == "done"
