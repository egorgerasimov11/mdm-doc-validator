"""M1: crashed documents count in every metric denominator. A pipeline that
fails on the hard docs must not score artificially clean — CRASH is never a
hit, costs like an NMR (fails loudly, ships nothing), and a crash on a gold
invoice counts as an invoice false-accept (the adoption gate requires 0)."""
import json
from types import SimpleNamespace

import pytest

from mdmdoc import config, evalrun
from mdmdoc.evalrun import CRASH, verdict_direction, verdict_metrics


# --- pure metric math --------------------------------------------------------
def test_verdict_metrics_crash_in_denominator():
    m = verdict_metrics([("ACCEPT", "ACCEPT"), (CRASH, "ACCEPT")])
    assert m["verdict_accuracy"] == 0.5


def test_verdict_metrics_crash_never_a_hit_even_on_gold_nmr():
    m = verdict_metrics([(CRASH, "NEED_MANUAL_REVIEW")])
    assert m["verdict_accuracy"] == 0.0
    assert m["verdict_cost"] == 0.0            # NMR-equivalent outcome: no cost
    assert m["unsafe_error_rate"] == 0.0


def test_verdict_metrics_crash_cost_is_nmr_equivalent():
    # gold REJECT: crash ranks NMR -> unsafe gap 1 -> cost 3 (NOT gap-3 like ACCEPT)
    m = verdict_metrics([(CRASH, "REJECT")])
    assert m["unsafe_error_rate"] == 1.0 and m["verdict_cost"] == 3.0
    # gold ACCEPT: crash is stricter -> safe gap 2 -> cost 2
    m = verdict_metrics([(CRASH, "ACCEPT")])
    assert m["safe_disagreement_rate"] == 1.0 and m["verdict_cost"] == 2.0
    assert verdict_direction(CRASH, "REJECT") == "unsafe"
    assert verdict_direction(CRASH, "ACCEPT") == "safe"
    assert verdict_direction(CRASH, "NEED_MANUAL_REVIEW") == ""


# --- run_eval integration (no Ollama: run_check is faked) --------------------
@pytest.fixture()
def eval_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "FEWSHOT_DIR", tmp_path / "fewshot")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "lora")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "corpus")
    (tmp_path / "corpus").mkdir()
    (tmp_path / "dataset").mkdir()
    return tmp_path


def _label(doc_path, doc_type="bank_letter", verdict="ACCEPT", scenarios=()):
    return {"doc_sha256": "deadbeef" + doc_path[:8].ljust(8, "x"),
            "doc_path": doc_path, "doc_class": "bank", "doc_type_gold": doc_type,
            "verdict_gold": verdict, "fields_gold": {}, "confirmed": True,
            "scenarios": list(scenarios), "sensitive_map": []}


def _write_labels(tmp_path, labels):
    config.LABELS_PATH.write_text(
        "\n".join(json.dumps(l) for l in labels) + "\n", encoding="utf-8")


def _fake_ok(verdict="ACCEPT", doc_type="bank_letter"):
    return SimpleNamespace(
        pub={"doc_type": doc_type, "json_valid_first_try": True, "fields": {},
             "tier": "fast"},
        verdict=verdict, run_id="cafe" * 4, findings=[])


def test_run_eval_crash_counts_and_reports(eval_env, monkeypatch, capsys):
    for f in ("a.pdf", "b.pdf", "c.pdf"):
        (config.CORPUS_DIR / f).write_bytes(b"x")
    _write_labels(eval_env, [
        _label("a.pdf", scenarios=["s1"]),
        _label("b.pdf", verdict="NEED_MANUAL_REVIEW", scenarios=["s1"]),
        _label("c.pdf"),
    ])

    def fake_run_check(path, doc_class, **kw):
        if path.name == "b.pdf":
            raise RuntimeError("boom on the hard doc")
        return _fake_ok()

    monkeypatch.setattr(evalrun, "run_check", fake_run_check)
    rc = evalrun.run_eval(record=False)
    assert rc == 0
    res = json.loads((config.EVAL_DIR / "last_results.json").read_text())
    m = res["metrics"]
    assert m["n"] == 3 and m["n_scored"] == 2
    assert m["crashes"] == 1 and m["crash_rate"] == round(1 / 3, 3)
    assert m["verdict_accuracy"] == round(2 / 3, 3)     # crash is a miss
    assert m["doc_type_accuracy"] == round(2 / 3, 3)
    # crash on a gold-NMR doc: no unsafe, no cost, still a miss
    assert m["unsafe_error_rate"] == 0.0
    # crashed doc never enters the gold-review label queue
    assert "b.pdf" not in res["gold_review"]
    # crash participates in scenario slices as regression signal
    assert m["scenarios"]["s1"]["n"] == 2
    assert m["scenarios"]["s1"]["verdict_accuracy"] == 0.5
    out = capsys.readouterr().out
    assert "crashed: 1" in out and "ERROR" in out


def test_run_eval_crash_on_invoice_counts_false_accept(eval_env, monkeypatch):
    (config.CORPUS_DIR / "inv.pdf").write_bytes(b"x")
    _write_labels(eval_env, [_label("inv.pdf", doc_type="invoice", verdict="REJECT")])
    monkeypatch.setattr(evalrun, "run_check",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("die")))
    evalrun.run_eval(record=False)
    m = json.loads((config.EVAL_DIR / "last_results.json").read_text())["metrics"]
    assert m["invoice_false_accept_rate"] == 1.0        # adoption gate must refuse
    assert m["unsafe_error_rate"] == 1.0                # crash vs gold REJECT


def test_run_eval_missing_file_skipped_not_crashed(eval_env, monkeypatch):
    (config.CORPUS_DIR / "a.pdf").write_bytes(b"x")
    _write_labels(eval_env, [_label("a.pdf"), _label("ghost.pdf")])
    monkeypatch.setattr(evalrun, "run_check", lambda *a, **k: _fake_ok())
    evalrun.run_eval(record=False)
    m = json.loads((config.EVAL_DIR / "last_results.json").read_text())["metrics"]
    assert m["n"] == 1 and m["skipped_missing"] == 1 and m["crashes"] == 0
    assert m["verdict_accuracy"] == 1.0                 # missing != crash
