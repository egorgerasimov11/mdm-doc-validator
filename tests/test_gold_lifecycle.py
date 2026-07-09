"""M7: gold-label lifecycle metadata + the staleness/active-learning signal."""
import json

import pytest

from mdmdoc import config
from mdmdoc.evalrun import gold_staleness_ranking, update_gold_staleness


@pytest.fixture()
def eval_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path)
    return tmp_path


def _row(file, verdict_ok, direction=""):
    return {"file": file, "verdict_ok": verdict_ok,
            "verdict_direction": direction}


def _lab(file, sha, confirmed):
    return {"doc_path": file, "doc_sha256": sha, "last_confirmed_ts": confirmed}


def test_staleness_accumulates_and_resets_on_reconfirm(eval_dir):
    labs = {"a.pdf": _lab("a.pdf", "sha-a", "2026-07-01T00:00:00Z")}
    update_gold_staleness([_row("a.pdf", False, "safe")], labs)
    update_gold_staleness([_row("a.pdf", False, "unsafe")], labs)
    state = json.loads((eval_dir / "gold_staleness.json").read_text())
    assert state["sha-a"]["disagreements_since_confirm"] == 2
    assert state["sha-a"]["safe"] == 1 and state["sha-a"]["unsafe"] == 1
    # operator re-confirms -> counters reset, then a new disagreement counts fresh
    labs["a.pdf"]["last_confirmed_ts"] = "2026-07-08T00:00:00Z"
    update_gold_staleness([_row("a.pdf", False, "safe")], labs)
    state = json.loads((eval_dir / "gold_staleness.json").read_text())
    assert state["sha-a"]["disagreements_since_confirm"] == 1
    assert state["sha-a"]["unsafe"] == 0


def test_staleness_ignores_crashes_and_agreements(eval_dir):
    labs = {"a.pdf": _lab("a.pdf", "sha-a", "t1"), "b.pdf": _lab("b.pdf", "sha-b", "t1")}
    rows = [{"file": "a.pdf", "verdict_ok": True, "verdict_direction": ""},
            {"file": "b.pdf", "error": "boom", "crash": True,
             "verdict_ok": False, "verdict_direction": ""}]
    state = update_gold_staleness(rows, labs)
    assert state["sha-a"]["disagreements_since_confirm"] == 0
    assert "sha-b" not in state


def test_ranking_orders_by_count_then_unsafe(eval_dir):
    labs = {f: _lab(f, f"sha-{f}", "t1") for f in ("a.pdf", "b.pdf", "c.pdf")}
    update_gold_staleness([_row("a.pdf", False, "safe"),
                           _row("b.pdf", False, "unsafe"),
                           _row("c.pdf", True)], labs)
    update_gold_staleness([_row("a.pdf", False, "safe")], labs)
    state = json.loads((eval_dir / "gold_staleness.json").read_text())
    rank = gold_staleness_ranking(state)
    assert [e["file"] for e in rank] == ["a.pdf", "b.pdf"]   # 2 disagreements first
    # equal counts -> unsafe first
    update_gold_staleness([_row("b.pdf", False, "unsafe")], labs)
    state = json.loads((eval_dir / "gold_staleness.json").read_text())
    rank = gold_staleness_ranking(state)
    assert rank[0]["file"] == "b.pdf" or rank[0]["unsafe"] >= rank[1].get("unsafe", 0)


def test_build_label_lifecycle_metadata(tmp_path, monkeypatch):
    """First confirm stamps label_ts; re-confirm keeps it and bumps the count."""
    from mdmdoc import review_core
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "dataset" / "mlx-lora")
    rid = "abcd1234abcd1234"
    d = tmp_path / "runs" / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "path": str(tmp_path / "doc.pdf"), "file_name": "doc.pdf",
        "doc_class": "w9", "run_id": rid, "ts": "2026-07-02T00:00:00Z"}))
    (d / "extraction.json").write_text(json.dumps({
        "doc_class": "w9", "doc_type": "w9",
        "fields": {"line1_name": "John Smith", "signed": True}}))
    (d / "stage_a.json").write_text(json.dumps({"raw_text_excerpt": "W-9 John Smith"}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))

    label1, _ = review_core.build_label(rid, {"fields": {}})
    assert label1["confirm_count"] == 1
    assert label1["label_ts"] == label1["ts"] or label1["label_ts"]
    from mdmdoc.dataset import append_label
    append_label(label1)
    label2, _ = review_core.build_label(rid, {"fields": {}})
    assert label2["confirm_count"] == 2
    assert label2["label_ts"] == label1["label_ts"]     # first-labeled time survives