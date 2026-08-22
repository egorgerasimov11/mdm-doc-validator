"""Scoring from cells on disk: version hygiene, document time, ranking, decision."""
import json

import fitz
import pytest

from mdmdoc import config
from mdmdoc.bench import manifest, metrics as M, report, run as R
from mdmdoc.extract import engines as E, render


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BENCH_DIR", tmp_path / "bench")
    from mdmdoc import ocr
    monkeypatch.setattr(ocr, "HAVE_TESSERACT", False)
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"Name: Acme {i}", fontsize=11)
        pg.insert_text((72, 100), f"Account: 48302291007{i}", fontsize=11)
        pg.insert_text((72, 128), "This letter confirms that the account above is held with our bank.", fontsize=11)
    pdf = tmp_path / "three.pdf"
    doc.save(pdf)
    doc.close()
    manifest.add([pdf], tags=["core"], pages=[0, 1, 2], stratum="synthetic", gold_source="textlayer")
    return tmp_path


class FakeEngine(E.PageEngine):
    family = "fake"
    render = render.PRESETS["q120"]

    def __init__(self, version="1", latency=1.0, text=None, eid="fake:test"):
        self.id = eid
        self.version = version
        self.latency = latency
        self.text = text

    def setup(self):
        pass

    def teardown(self):
        pass

    def transcribe(self, job):
        text = self.text if self.text is not None else manifest.text_layer(manifest.load("all")[0], job.page)
        return E.PageResult(text=text, latency_s=self.latency, meta={})


def test_score_tag_uses_newest_version_only_and_warns(bench, capsys):
    docs = manifest.load("all")
    R.run_sweep([FakeEngine(version="1")], docs, "t", force=True)
    R.run_sweep([FakeEngine(version="2")], docs, "t", pages_cap=1)     # partial re-run
    scored = report.score_tag("t", docs)
    data = scored["fake:test"]
    # p0 was overwritten by the v2 run; p1/p2 still carry v1 and must NOT be mixed in
    assert data["versions"] == {"1": 2, "2": 1} and data["stale_cells"] == 2
    assert sum(len(v["pages"]) for v in data["docs"].values()) == 1
    assert "stale cell" in capsys.readouterr().err


def test_doc_time_in_leaderboard_and_extrapolation(bench):
    docs = manifest.load("all")
    d = docs[0]
    d.pages = [0, 1]                                    # measure 2 of 3 pages
    R.run_sweep([FakeEngine(latency=20.0)], [d], "t", force=True)
    scored = report.score_tag("t", docs)
    agg = scored["fake:test"]["docs"][d.doc_id]["agg"]
    assert agg["doc_time_s"] == 60.0 and agg["extrapolated"]
    table = report.slice_table(scored, docs)
    a = table["synthetic"]["fake:test"]
    assert a["doc_time_median_s"] == 60.0 and a["within_60s_share"] == 1.0 and a["pass"]
    md = report.leaderboard_md("t", table, scored, docs)
    assert "doc time (median · p90 · ≤60s)" in md and "60s · 60s · 100%*" in md
    assert "v=1:2" in md


def test_slow_engine_fails_and_fast_ranks_first(bench):
    docs = manifest.load("all")
    R.run_sweep([FakeEngine(latency=30.0, eid="fake:slow")], docs, "t", force=True)
    R.run_sweep([FakeEngine(latency=1.0, eid="fake:fast")], docs, "t", force=True)
    scored = report.score_tag("t", docs)
    table = report.slice_table(scored, docs)
    slow, fast = table["synthetic"]["fake:slow"], table["synthetic"]["fake:fast"]
    assert slow["doc_time_median_s"] == 90.0 and not slow["pass"] and any("doc_time_p90" in f for f in slow["fails"])
    assert fast["pass"]
    md = report.leaderboard_md("t", table, scored, docs)
    assert md.index("`fake:fast`") < md.index("`fake:slow`")
    dm = report.decision_md("t", {"real": table["synthetic"], "handwriting": {}}, None, docs)
    assert "latency ≤ 90" not in dm and "`fake:fast`" in dm and "within 60 s" in dm


def test_layer_plus_engine_latency_is_the_sum(bench):
    docs = manifest.load("all")
    R.run_sweep([E.TextLayerEngine()], docs, "t", force=True)
    R.run_sweep([FakeEngine(latency=10.0, eid="fake:vlm")], docs, "t", force=True)
    scored = report.score_tag("t", docs)
    d = docs[0]
    tl = scored["textlayer"]["docs"][d.doc_id]["agg"]["doc_time_s"]
    union = scored["layer+fake:vlm"]["docs"][d.doc_id]["agg"]["doc_time_s"]
    assert union == pytest.approx(tl + 30.0, abs=0.2)
    assert scored["layer>fake:vlm"]["docs"][d.doc_id]["agg"]["doc_time_s"] == tl


def test_latency_rows(bench):
    docs = manifest.load("all")
    R.run_sweep([FakeEngine(latency=25.0)], docs, "t", force=True)
    scored = report.score_tag("t", docs)
    rows = report.latency_rows(scored, "fake:test", docs)
    assert len(rows) == 1 and rows[0]["doc_time_s"] == 75.0 and not rows[0]["extrapolated"]
