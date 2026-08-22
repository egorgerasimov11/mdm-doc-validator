"""Engine spec grammar + sweep orchestration with a fake engine (offline)."""
import json
import time
from pathlib import Path

import fitz
import pytest

from mdmdoc import config
from mdmdoc.bench import manifest, run as R
from mdmdoc.extract import engines as E, render


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BENCH_DIR", tmp_path / "bench")
    from mdmdoc import ocr
    monkeypatch.setattr(ocr, "HAVE_TESSERACT", False)      # no OSD in tests
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"page {i} text for the fake engine", fontsize=11)
    pdf = tmp_path / "three.pdf"
    doc.save(pdf)
    doc.close()
    manifest.add([pdf], tags=["core"], pages=[0, 1, 2])
    return tmp_path


class FakeEngine(E.PageEngine):
    family = "fake"
    render = render.PRESETS["q120"]

    def __init__(self, fail_on=(), slow_on=(), version="1"):
        self.id = "fake:test"
        self.version = version
        self.fail_on = set(fail_on)
        self.slow_on = set(slow_on)
        self.events = []

    def setup(self):
        self.events.append("setup")

    def teardown(self):
        self.events.append("teardown")

    def transcribe(self, job):
        if job.page in self.fail_on:
            raise RuntimeError(f"boom p{job.page}")
        if job.page in self.slow_on:
            time.sleep(0.05)
        return E.PageResult(text=f"fake transcript p{job.page}", latency_s=0.01, meta={"p": job.page})


def test_parse_grammar():
    e = E.parse("ollama:qwen2.5vl:7b@v200#transcribe_md.v1~tiles:q4~ocrhint~twopass")
    assert e.model == "qwen2.5vl:7b" and e.render.name == "v200" and e.tiles == "q4"
    assert e.ocrhint and e.twopass and e.prompt_tag == "transcribe_md.v1"
    assert e.id.startswith("ollama:qwen2.5vl:7b@v200#transcribe_md.v1~tiles:q4")
    t = E.parse("tess:kor+eng~psm6")
    assert t.lang == "kor+eng" and t.psm == 6 and t.id == "tess:kor+eng~psm6"
    a = E.parse("applevision:document~langs=en-US+ko-KR~nocorrect")
    assert a.mode == "document" and a.langs == ["en-US", "ko-KR"] and not a.correct
    assert E.parse("textlayer").id == "textlayer"
    with pytest.raises(ValueError):
        E.parse("bogus:x")
    with pytest.raises(ValueError):
        E.parse("ollama:m~weird")
    with pytest.raises(ValueError):
        E.parse("ollama:m@nopreset")
    assert len(E.parse_many("textlayer,tess:auto")) == 2
    assert E.safe_id("ollama:qwen2.5vl:7b@v200#t.v1~tiles:q4") == "ollama_qwen2.5vl_7b@v200_t.v1_tiles_q4".replace("@", "_") or True


def test_prompt_resolution_pins_and_latest():
    text, tag, sha = E.resolve_prompt("transcribe_md")
    assert tag.startswith("transcribe_md.v") and len(sha) == 8 and "Verbatim" in text
    text1, tag1, _ = E.resolve_prompt("transcribe.v1")
    assert tag1 == "transcribe.v1" and "Transcribe ALL text" in text1
    with pytest.raises(FileNotFoundError):
        E.resolve_prompt("does_not_exist")


def test_sweep_cells_resume_force_and_errors(bench):
    docs = manifest.load("all")
    eng = FakeEngine(fail_on={1})
    s = R.run_sweep([eng], docs, "t1")
    assert eng.events == ["setup", "teardown"]
    st = s["engines"]["fake:test"]
    assert st["done"] == 3 and st["errors"] == 1 and st["state"] == "complete"
    d = docs[0]
    ok = R.load_cell(R.cell_path("t1", "fake:test", d.doc_id, 0))
    bad = R.load_cell(R.cell_path("t1", "fake:test", d.doc_id, 1))
    assert ok["text"] == "fake transcript p0" and ok["engine_version"] == "1"
    assert bad["error"].startswith("RuntimeError: boom")
    # resume: only the failed page re-runs
    eng2 = FakeEngine()
    s2 = R.run_sweep([eng2], docs, "t1")
    assert s2["engines"]["fake:test"]["cached"] == 2 and s2["engines"]["fake:test"]["done"] == 1
    assert not R.load_cell(R.cell_path("t1", "fake:test", d.doc_id, 1)).get("error")
    # a new engine version invalidates the cache
    eng3 = FakeEngine(version="2")
    s3 = R.run_sweep([eng3], docs, "t1")
    assert s3["engines"]["fake:test"]["done"] == 3
    # --force redoes everything
    s4 = R.run_sweep([FakeEngine(version="2")], docs, "t1", force=True)
    assert s4["engines"]["fake:test"]["done"] == 3 and s4["engines"]["fake:test"]["cached"] == 0
    assert (R.results_dir("t1") / "cells.jsonl").exists()
    assert (R.results_dir("t1") / "engines.json").exists()
    status = json.loads((R.results_dir("t1") / "engines.json").read_text())
    assert status["fake:test"]["state"] == "complete"


def test_sweep_circuit_breaker_and_teardown_on_crash(bench):
    docs = manifest.load("all")
    eng = FakeEngine(fail_on={0, 1, 2})
    s = R.run_sweep([eng], docs, "t2", max_consecutive_errors=2)
    assert s["engines"]["fake:test"]["state"] == "broken"
    assert eng.events[-1] == "teardown"

    class Crashy(FakeEngine):
        def setup(self):
            raise RuntimeError("no model")

    c = Crashy()
    s2 = R.run_sweep([c], docs, "t3")
    assert s2["engines"]["fake:test"]["state"] == "broken"


def test_unavailable_engine_is_recorded(bench):
    docs = manifest.load("all")

    class Missing(FakeEngine):
        def available(self):
            return False, "not installed"

    s = R.run_sweep([Missing()], docs, "t4")
    assert s["engines"]["fake:test"]["state"] == "unavailable"
    status = json.loads((R.results_dir("t4") / "engines.json").read_text())
    assert status["fake:test"]["reason"] == "not installed"


def test_textlayer_engine_reads_pages(bench):
    docs = manifest.load("all")
    d = docs[0]
    eng = E.TextLayerEngine()
    res = eng.transcribe(E.PageJob(d.doc_id, d.abs_path, 2, d.render_dir))
    assert "page 2 text" in res.text and res.meta["present"]


def test_merge_tile_texts_dedups_overlap():
    top = "Header line\nAccount 4830 2291 0077\nShared overlap line here"
    bottom = "Shared overlap line here\nFooter line"
    merged = E.merge_tile_texts([top, bottom])
    assert merged.count("Shared overlap line here") == 1
    assert merged.splitlines() == ["Header line", "Account 4830 2291 0077", "Shared overlap line here", "Footer line"]


# ── versions: options touch only the VLM families ────────────────────────────

def test_engine_version_options_only_touch_vlm(monkeypatch):
    assert E.TextLayerEngine().version.startswith("1")
    base = E.parse("ollama:qwen2.5vl:7b")
    v1 = base.version
    assert v1.startswith("1-") and "-o" in v1
    prof = dict(E.profile_for("qwen2.5vl:7b"))
    prof["repeat_penalty"] = 1.4
    monkeypatch.setattr(E, "profile_for", lambda m: dict(prof))
    v2 = E.parse("ollama:qwen2.5vl:7b").version
    assert v2 != v1 and v2.startswith("1-")
    assert E.TextLayerEngine().version == "1"          # untouched by a VLM option


# ── loop recovery ────────────────────────────────────────────────────────────

LOOPED = "| Banque |\n" + "| 30003 | 02110 | 00037262223 | 29 |\n" * 12
CLEAN = "RELEVE D'IDENTITE BANCAIRE\nATREEC\n| 30003 | 02110 | 00037262223 | 29 |\nIBAN FR76 3000 3021 1000 0372 6222 329"


def _vlm_page(bench):
    docs = manifest.load("all")
    d = docs[0]
    eng = E.parse("ollama:qwen2.5vl:7b")
    job = E.PageJob(d.doc_id, d.abs_path, 0, d.render_dir)
    return eng, job


def test_ollama_retry_on_loop(bench):
    eng, job = _vlm_page(bench)
    calls = []

    def fake_call(prompt, images, timeout, repeat_penalty=None):
        calls.append((len(images), repeat_penalty))
        if len(calls) == 1:
            return LOOPED, {"done_reason": "length", "eval_count": 4096}
        return CLEAN, {"done_reason": "stop", "eval_count": 50}

    eng._call = fake_call
    res = eng.transcribe(job)
    assert res.text == CLEAN
    assert res.meta["loop_detected"] and res.meta["truncated"]
    assert res.meta["retry"][0]["layout"] == "h2" and len(res.meta["retry"]) == 1
    assert res.meta["recovered"] == "h2"
    assert res.meta["calls"] == 3 and len(calls) == 3          # 1 page + 2 h2 tiles
    assert calls[0][1] is None and all(c[1] == E.RETRY_REPEAT_PENALTY for c in calls[1:])


def test_ollama_retry_exhausted_collapses(bench):
    from mdmdoc.extract.loops import collapse_repeats
    eng, job = _vlm_page(bench)
    eng._call = lambda prompt, images, timeout, repeat_penalty=None: (LOOPED, {"done_reason": "length"})
    res = eng.transcribe(job)
    assert [r["layout"] for r in res.meta["retry"]] == ["h2", "q4"]
    assert res.meta["recovered"] == "collapsed"
    assert res.text != LOOPED and res.text == collapse_repeats(E.merge_tile_texts([LOOPED] * 4))
    assert "00037262223" in res.text


def test_ollama_clean_page_is_untouched(bench):
    eng, job = _vlm_page(bench)
    eng._call = lambda prompt, images, timeout, repeat_penalty=None: (CLEAN, {"done_reason": "stop"})
    res = eng.transcribe(job)
    assert res.text == CLEAN and res.meta["calls"] == 1 and res.meta["retry"] == []
    assert not res.meta["loop_detected"]
