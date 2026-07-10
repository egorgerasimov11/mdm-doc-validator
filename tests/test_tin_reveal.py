"""The operator console shows tax numbers in full (config.tin_values_policy).

This file locks the four channels the reveal must NOT reach. Each one is a
separate mechanism, so each gets its own test: a future refactor that makes the
display policy leak into any of them fails here, not on a real document.

  1. reasoning.md  — built for export to an external LLM: masked by construction
  2. training data — labels / few-shot / LoRA: gated strict, no 'value' key ever
  3. outbound web  — egress forbids TIN_KINDS unconditionally
  4. BTP/api-only  — the SAP-facing deployment stays masked
"""
import json

import pytest

from mdmdoc import config, review_core
from mdmdoc.fields import Extraction
from mdmdoc.privacy import (SecretVault, assert_no_leak, display_value, mask,
                            tin_visible)

TIN = "81-0826734"
SSN = "320-54-0693"
IBAN = "DE44500105175407324931"


# ------------------------------------------------------------------ policy ----
def test_policy_matrix(monkeypatch):
    monkeypatch.delenv("MDMDOC_BANK_VALUES", raising=False)
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
    monkeypatch.delenv("MDMDOC_MODE", raising=False)
    assert config.tin_values_policy() == "full"      # operator console
    assert config.gate_policy() == "none"

    monkeypatch.setenv("MDMDOC_TIN_VALUES", "masked")
    assert config.gate_policy() == "tin-only"

    monkeypatch.setenv("MDMDOC_BANK_VALUES", "masked")
    assert config.gate_policy() == "strict"


def test_api_only_masks_everything(monkeypatch):
    """The BTP/SAP-facing deployment never reveals either family."""
    monkeypatch.delenv("MDMDOC_BANK_VALUES", raising=False)
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
    monkeypatch.setenv("MDMDOC_MODE", "api-only")
    assert config.tin_values_policy() == "masked"
    assert config.bank_values_policy() == "masked"
    assert config.gate_policy() == "strict"
    assert display_value("tin", TIN, "full") == "XX-XXX6734"
    assert not tin_visible("full")


# ------------------------------------------------------- 1. reasoning.md ------
def test_masked_policy_is_never_overridden(monkeypatch):
    """reasoning.md is built from to_public(policy='masked') and then scrubbed.
    Whatever the console is configured to show, that path must stay masked."""
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)   # console default: full
    w = Extraction(doc_class="w9", doc_type="w9")
    w.fields = {"tin_type": "SSN", "tin_raw": SSN}
    pub = w.to_public(policy="masked")
    blob = json.dumps(pub)
    assert "value" not in pub["fields"]["tin"]
    assert SSN not in blob and "320540693" not in blob
    assert not tin_visible("masked")


def test_reasoning_artifact_survives_the_strict_scrub(monkeypatch):
    """Even if a full TIN reached the reasoning text, the strict scrub removes it."""
    from mdmdoc.privacy import scrub_text
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
    vault = SecretVault()
    vault.register("tin", SSN)
    out = scrub_text(f"the taxpayer gave {SSN} on page 1", vault, policy="strict")
    assert SSN not in out


# ------------------------------------------------------- 2. training data -----
@pytest.fixture()
def revealed_run(tmp_path, monkeypatch):
    """A run whose extraction.json carries the full TIN — what the console now
    writes. The label built from it must still be masked."""
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
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
                           "hyphenated": True, "present": True, "value": SSN},
                   "address_street": "1 Main St", "address_city_state_zip": "",
                   "signed": True, "sign_date": ""}}))
    (d / "stage_a.json").write_text(json.dumps({"raw_text_excerpt": "Form W-9 John Smith",
                                                "regex_candidates_masked": {}}))
    (d / "report.json").write_text(json.dumps({"verdict": "ACCEPT"}))
    return rid


def test_review_form_shows_the_full_tin(revealed_run):
    """The operator corrects against what they can actually read."""
    form = review_core.review_defaults(revealed_run)
    assert form["display"]["tin_raw"] == SSN


def test_keep_path_never_writes_the_value_into_a_label(revealed_run):
    """'Mark valid' / keep copies the public entry — build_label must strip
    'value', or every confirmation would push a real SSN into labels.jsonl."""
    label, secrets = review_core.build_label(revealed_run, {"fields": {}})
    tin = label["fields_gold"]["tin"]
    assert tin["present"] and tin["masked"] == "XXX-XX-0693"
    assert "value" not in tin
    blob = json.dumps(label)
    assert SSN not in blob and "320540693" not in blob
    assert_no_leak(blob, secrets, allowed_fakes=[label["sensitive_map"][0]["fake"]])


def test_set_path_masks_a_typed_tin(revealed_run):
    """The operator types a corrected TIN into the form: the label stores the
    mask, and BOTH sides of the recorded diff are masked."""
    label, secrets = review_core.build_label(
        revealed_run, {"fields": {"tin_raw": {"action": "set", "value": TIN},
                                  "tin_type": {"action": "set", "value": "EIN"}}})
    assert label["fields_gold"]["tin"]["masked"] == "XX-XXX6734"
    assert "value" not in label["fields_gold"]["tin"]
    diff = label["model_predicted"]["fields_diff"]["tin_raw"]
    assert diff == {"model": "XXX-XX-0693", "gold": "XX-XXX6734"}
    assert secrets == [TIN]                      # the typed value is a known secret
    blob = json.dumps(label)
    assert TIN not in blob and SSN not in blob


def test_label_write_is_gated_strict_even_on_the_console(monkeypatch):
    from mdmdoc import dataset
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
    label = {"doc_sha256": "x", "fields_gold": {"tin": {"masked": mask("tin", TIN),
                                                        "value": TIN, "present": True}}}
    with pytest.raises(ValueError):
        dataset.append_label(label, [TIN])


# ------------------------------------------------------- 3. outbound web ------
def test_egress_still_forbids_tin(monkeypatch):
    from mdmdoc.web_enrichment import egress
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
    vault = SecretVault()
    vault.register("tin", SSN)
    vault.register("iban", IBAN)
    assert SSN in egress.forbidden_secrets(vault)


# ------------------------------------------------------- 4. run artifacts -----
def test_gate_none_lets_a_revealed_run_artifact_through(monkeypatch):
    monkeypatch.delenv("MDMDOC_BANK_VALUES", raising=False)
    monkeypatch.delenv("MDMDOC_TIN_VALUES", raising=False)
    monkeypatch.delenv("MDMDOC_MODE", raising=False)
    blob = f"TIN (EIN): {TIN}  account 1408137817  iban {IBAN}"
    assert assert_no_leak(blob, [], policy=config.gate_policy()) == []
    # ...while the same blob is still a leak on any training-data write
    with pytest.raises(ValueError):
        assert_no_leak(blob, policy="strict")
