"""Closed learning loop: scenario tags, error_source, eval slices, few-shot
coverage selection, adoption gate, training queue."""
import json

import pytest

from mdmdoc import config, review_core, scenarios
from mdmdoc.adoption import gate_check
from mdmdoc.evalrun import scenario_slices
from mdmdoc.fewshot import _pick_by_coverage
from mdmdoc.training_queue import build_queue


# ---------------------------------------------------------------- scenarios ---
def test_normalize_tags_dedupes_and_snake_cases():
    assert scenarios.normalize_tags([" W9 Boxed-TIN ", "w9_boxed_tin", "", None]) == \
        ["w9_boxed_tin"]


def test_suggest_w9_tags():
    tags = scenarios.suggest(
        {"doc_class": "w9"},
        {"has_text_layer": False, "rotations": {"0": 90},
         "regex_candidates_masked": {"tin_boxed": "XX-XXX0000"}},
        {"doc_type": "w9", "warnings": ["classification: visual checkbox = X, text said Y"],
         "fields": {"signed": False}},
        [{"rule_id": "W9-013"}])
    assert set(tags) == {"w9_image_only", "w9_rotated_photo", "w9_boxed_tin",
                         "w9_checkbox_error", "w9_line_swap", "w9_unsigned"}


def test_suggest_bank_mixed_packet_and_officer_block():
    tags = scenarios.suggest(
        {"doc_class": "bank", "sap_path": "/x/sap.png"},
        {"has_text_layer": True, "bank_letter_pages": [2], "invoice_pages": [0],
         "regex_candidates_masked": {"routing_aba": "1234", "routing_aba_wires": "5678"}},
        {"doc_type": "bank_letter"},
        [{"rule_id": "BNK-026"}])
    assert set(tags) == {"bank_invoice_plus_letter", "bank_typed_officer_block",
                         "bank_multi_aba", "bank_sap_compare"}


# ---------------------------------------------------------------- review v2 ---
@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "dataset" / "mlx-lora")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    rid = "abcd1234abcd1234"
    d = tmp_path / "runs" / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "path": str(tmp_path / "doc.pdf"), "file_name": "doc.pdf",
        "doc_class": "w9", "run_id": rid, "ts": "2026-07-03T00:00:00Z"}))
    (d / "extraction.json").write_text(json.dumps({
        "doc_class": "w9", "doc_type": "w9",
        "fields": {"line1_name": "John Smith", "line2_business_name": "",
                   "line3_classification": "Individual/sole proprietor",
                   "tin": {"type": "SSN", "masked": "XXX-XX-0693", "digits": 9,
                           "hyphenated": True, "present": True},
                   "address_street": "1 Main St", "address_city_state_zip": "",
                   "signed": True, "sign_date": ""}}))
    (d / "stage_a.json").write_text(json.dumps({
        "has_text_layer": False,
        "raw_text_excerpt": "Form W-9 ... John Smith",
        "regex_candidates_masked": {"tin_boxed": "XXX-XX-0693"}}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))
    return rid


def test_review_defaults_offer_scenarios(run_env):
    form = review_core.review_defaults(run_env)
    assert "w9_boxed_tin" in form["scenario_options"]
    assert "w9_image_only" in form["scenarios_suggested"]
    assert "w9_boxed_tin" in form["scenarios_suggested"]
    assert "ocr_missed" in form["error_sources"]


def test_label_stores_scenarios_and_error_source(run_env):
    label, _ = review_core.build_label(run_env, {
        "fields": {}, "scenarios": ["W9 Boxed-TIN", "w9_image_only"],
        "error_source": "ocr_missed"})
    assert label["scenarios"] == ["w9_boxed_tin", "w9_image_only"]
    assert label["error_source"] == "ocr_missed"


def test_label_rejects_unknown_error_source(run_env):
    label, _ = review_core.build_label(run_env, {"fields": {},
                                                 "error_source": "gremlins"})
    assert label["error_source"] == ""


# ---------------------------------------------------------------- eval slices -
def test_scenario_slices_aggregates_per_tag():
    rows = [
        {"ok": True, "type_ok": True, "verdict_ok": True, "scenarios": ["a", "b"]},
        {"ok": False, "type_ok": True, "verdict_ok": False, "scenarios": ["a"]},
        {"error": "boom", "scenarios": ["a"]},
        {"ok": True, "type_ok": True, "verdict_ok": True, "scenarios": []},
    ]
    s = scenario_slices(rows)
    assert s["a"] == {"n": 2, "accuracy": 0.5, "doc_type_accuracy": 1.0,
                      "verdict_accuracy": 0.5}
    assert s["b"]["n"] == 1 and s["b"]["accuracy"] == 1.0
    assert set(s) == {"a", "b"}


# ---------------------------------------------------------------- few-shot ----
def _lab(doc_type, scen, diff_n=0, complete=True):
    gold = {"line1_name": "X Corp" if complete else "",
            "line3_classification": "C corporation" if complete else "",
            "tin": {"present": complete, "masked": "XX-XXX1111"}}
    return {"doc_class": "w9", "doc_type_gold": doc_type, "scenarios": scen,
            "fields_gold": gold, "confirmed": True,
            "model_predicted": {"doc_type": doc_type,
                                "fields_diff": {f"f{i}": {} for i in range(diff_n)}}}


def test_pick_by_coverage_prefers_new_scenarios_over_teaching_value():
    covered_twice = _lab("w9", ["w9_boxed_tin"], diff_n=9)   # high teaching value
    fresh = _lab("w9", ["w9_checkbox_error"], diff_n=0)      # new scenario, low value
    picked = _pick_by_coverage([covered_twice, _lab("w9", ["w9_boxed_tin"], diff_n=8),
                                fresh], k=2)
    assert picked[0] is covered_twice          # best teaching value first
    assert picked[1] is fresh                  # coverage beats the second boxed_tin


def test_pick_by_coverage_falls_back_to_doc_type_diversity():
    a = _lab("w9", [], diff_n=5)
    b = _lab("w8", [], diff_n=0)
    c = _lab("w9", [], diff_n=4)
    picked = _pick_by_coverage([a, b, c], k=2)
    assert picked == [a, b]                    # second pick covers type:w8, not c


# ---------------------------------------------------------------- gate --------
BASE = {"leakage_count": 0, "invoice_false_accept_rate": 0,
        "fields": {"w9.tin": 1.0, "bank.iban": 0.9}}


def test_gate_passes_clean_candidate():
    ok, reasons = gate_check({"leakage_count": 0, "invoice_false_accept_rate": None,
                              "fields": {"w9.tin": 1.0, "bank.iban": 0.9}}, BASE)
    assert ok and reasons == []


def test_gate_fails_on_leak_invoice_and_critical_regression():
    ok, reasons = gate_check({"leakage_count": 2, "invoice_false_accept_rate": 0.5,
                              "fields": {"w9.tin": 0.5, "bank.iban": 0.9}}, BASE)
    assert not ok
    assert any("leakage" in r for r in reasons)
    assert any("invoice" in r for r in reasons)
    assert any("w9.tin" in r for r in reasons)


def test_gate_ignores_non_critical_regressions():
    base = dict(BASE, fields={"w9.tin": 1.0, "w9.line2_business_name": 1.0})
    cand = {"leakage_count": 0, "invoice_false_accept_rate": 0,
            "fields": {"w9.tin": 1.0, "w9.line2_business_name": 0.5}}
    ok, reasons = gate_check(cand, base)
    assert ok and reasons == []


def test_gate_without_baseline_uses_absolute_conditions_only():
    ok, _ = gate_check({"leakage_count": 0, "invoice_false_accept_rate": 0,
                        "fields": {"w9.tin": 0.1}}, None)
    assert ok


# ---------------------------------------------------------------- queue -------
def _mk_run(tmp_path, rid, verdict, doc_class="bank", warnings=None,
            escalated=None, stage_a=None, ts="2026-07-03T00:00:00Z"):
    d = config.RUNS_DIR / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "path": str(tmp_path / f"{rid}.pdf"), "file_name": f"{rid}.pdf",
        "doc_class": doc_class, "run_id": rid, "ts": ts,
        "escalated_because": escalated or []}))
    (d / "extraction.json").write_text(json.dumps({
        "doc_class": doc_class, "doc_type": "bank_letter",
        "warnings": warnings or [], "fields": {}}))
    (d / "stage_a.json").write_text(json.dumps(stage_a or {"has_text_layer": True}))
    (d / "findings.json").write_text("[]")
    (d / "report.json").write_text(json.dumps(
        {"verdict": verdict, "doc_type": "bank_letter"}))


def test_queue_ranks_high_signal_unlabeled_runs(run_env, tmp_path):
    _mk_run(tmp_path, "aaaa000000000001", "ACCEPT")   # boring — not queued
    _mk_run(tmp_path, "aaaa000000000002", "NEED_MANUAL_REVIEW",
            warnings=["tier disagreement: bank_name (fast=A, strong=B)"],
            escalated=["quality"])
    q = build_queue()
    ids = [r["run_id"] for r in q]
    assert "aaaa000000000002" in ids and "aaaa000000000001" not in ids
    top = q[0]
    assert top["run_id"] == "aaaa000000000002"
    assert any("manual review" in r for r in top["reasons"])
    assert any("escalated" in r for r in top["reasons"])
    assert any("conflict" in r for r in top["reasons"])


def test_queue_surfaces_uncovered_scenarios_and_eval_regressions(run_env, tmp_path):
    # unlabeled run showing a scenario no label covers
    _mk_run(tmp_path, "bbbb000000000001", "ACCEPT",
            stage_a={"has_text_layer": False})
    # labeled run that regressed in the last eval
    config.EVAL_DIR.mkdir(parents=True, exist_ok=True)
    review_core.submit_review(run_env, {"fields": {}, "scenarios": ["w9_boxed_tin"]})
    (config.EVAL_DIR / "last_results.json").write_text(json.dumps(
        {"rows": [], "diff": {"regressed": ["doc.pdf"], "unchanged_wrong": []}}))
    q = build_queue()
    by_id = {r["run_id"]: r for r in q}
    assert any("uncovered scenario: bank_image_only" in r
               for r in by_id["bbbb000000000001"]["reasons"])
    lab_row = by_id[run_env]
    assert lab_row["labeled"] and any("stale" in r for r in lab_row["reasons"])
