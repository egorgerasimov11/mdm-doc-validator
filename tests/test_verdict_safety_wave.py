"""Verdict-safety wave (audit-wave): fail-closed behaviors added after the
2026-07 audit — concurrent label writes, SAP-compare crash containment,
precedent relaxation gating."""
import json
import threading

import pytest

from mdmdoc import config, dataset


@pytest.fixture()
def labels_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path)
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "labels.jsonl")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "lora")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "FEWSHOT_DIR", tmp_path / "fewshot")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    return tmp_path


def test_concurrent_append_label_loses_nothing(labels_env):
    shas = [f"deadbeef{i:08x}" for i in range(8)]   # letters: not a digit-run leak
    labels = [{"doc_sha256": s, "doc_class": "bank",
               "doc_type_gold": "bank_letter", "sensitive_map": []}
              for s in shas]
    threads = [threading.Thread(target=dataset.append_label, args=(lab,))
               for lab in labels]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = config.LABELS_PATH.read_text(encoding="utf-8").strip().splitlines()
    rows = [json.loads(l) for l in lines]                    # every line parses
    assert {r["doc_sha256"] for r in rows} == set(shas)


def test_atomic_write_replaces_not_truncates(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("old", encoding="utf-8")
    config.atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert not p.with_name("state.json.tmp").exists()        # tmp cleaned up
