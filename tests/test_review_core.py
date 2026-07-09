import json

import pytest

from mdmdoc import config, review_core
from mdmdoc.privacy import assert_no_leak


@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    """A fabricated run dir + isolated dataset paths."""
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
        "fields": {"line1_name": "John Smith", "line2_business_name": "",
                   "line3_classification": "Individual/sole proprietor",
                   "tin": {"type": "SSN", "masked": "XXX-XX-0693", "digits": 9,
                           "hyphenated": True, "present": True},
                   "address_street": "1 Main St", "address_city_state_zip": "",
                   "signed": True, "sign_date": ""}}))
    (d / "stage_a.json").write_text(json.dumps({
        "raw_text_excerpt": "Form W-9 ... John Smith ... XXX-XX-0693",
        "regex_candidates_masked": {"ssn_masked": "***-**-0693"}}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))
    return rid


def test_defaults_shape(run_env):
    form = review_core.review_defaults(run_env)
    assert form["doc_class"] == "w9"
    assert "tin_raw" in form["sensitive"]
    assert form["display"]["line1_name"] == "John Smith"
    assert form["display"]["tin_raw"] == "XXX-XX-0693"
    assert form["verdict"] == "ACCEPT"


def test_keep_sensitive_synthesizes_fake(run_env):
    label, secrets = review_core.build_label(run_env, {"fields": {}})
    tin = label["fields_gold"]["tin"]
    assert tin["present"] and tin["masked"] == "XXX-XX-0693"
    assert secrets == []                      # nothing typed -> no real values seen
    smap = label["sensitive_map"]
    assert len(smap) == 1 and smap[0]["kind"] == "tin"
    fake = smap[0]["fake"]
    assert fake.count("-") == 2 and len(fake) == 11 and fake != "000-00-0000"
    assert_no_leak(json.dumps(label), allowed_fakes=[fake])


def test_verdict_confirmed_roundtrip(run_env):
    """C11: the explicit relax-confirmation flag persists on the label and
    defaults to False when the reviewer did not check it."""
    label, _ = review_core.build_label(run_env, {"fields": {}})
    assert label["verdict_confirmed"] is False
    label, _ = review_core.build_label(
        run_env, {"fields": {}, "verdict_gold": "ACCEPT", "verdict_confirmed": True})
    assert label["verdict_confirmed"] is True


def test_set_sensitive_masks_and_returns_secret(run_env):
    sub = {"fields": {"tin_raw": {"action": "set", "value": "123-45-6789"},
                      "tin_type": {"action": "set", "value": "SSN"}},
           "doc_type_gold": "w9", "verdict_gold": "ACCEPT"}
    label, secrets = review_core.build_label(run_env, sub)
    tin = label["fields_gold"]["tin"]
    assert tin == {"type": "SSN", "masked": "XXX-XX-6789", "digits": 9,
                   "hyphenated": True, "present": True}
    assert secrets == ["123-45-6789"]
    assert "123-45-6789" not in json.dumps(label)
    assert label["model_predicted"]["fields_diff"]["tin_raw"]["gold"] == "XXX-XX-6789"


def test_clear_sensitive(run_env):
    label, _ = review_core.build_label(
        run_env, {"fields": {"tin_raw": {"action": "clear"}}})
    assert label["fields_gold"]["tin"] == {"present": False, "masked": ""}


def test_plain_and_boolean_paths(run_env):
    sub = {"fields": {"line1_name": {"action": "set", "value": "Jane Doe"},
                      "signed": {"action": "set", "value": False},
                      "address_street": {"action": "clear"}}}
    label, _ = review_core.build_label(run_env, sub)
    g = label["fields_gold"]
    assert g["line1_name"] == "Jane Doe" and g["signed"] is False
    assert g["address_street"] == ""
    diff = label["model_predicted"]["fields_diff"]
    assert diff["line1_name"]["model"] == "John Smith"
    assert "line2_business_name" not in diff    # unchanged -> no diff entry


def test_submit_replaces_earlier_label(run_env):
    review_core.submit_review(run_env, {"fields": {}, "notes": "first"})
    res = review_core.submit_review(run_env, {"fields": {}, "notes": "second"})
    assert res["labels_count"] == 1
    from mdmdoc.dataset import load_labels
    assert load_labels()[0]["notes"] == "second"
