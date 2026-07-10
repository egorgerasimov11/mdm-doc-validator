"""D11: the closed learning loop — runtime few-shot for the production model,
notes become PENDING rules (additive-only), valid-marks feed the pattern
memory, thumbs feed the ledger + training queue. Invariant throughout: no
unapproved rule ever changes a verdict."""
import json

import pytest

from mdmdoc import config, learning, patterns, ratings
from mdmdoc.fields import Extraction
from mdmdoc.rules.engine import Finding


@pytest.fixture()
def data_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    (tmp_path / "rules").mkdir()
    return tmp_path


# --- D11a: runtime few-shot injection for mdmdoc-extract ------------------------
def test_fresh_exemplar_reaches_baked_model_prompt(monkeypatch, tmp_path):
    from mdmdoc import stage_b
    from mdmdoc.stage_a import RawDoc
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path)
    (tmp_path / "exemplar_values.json").write_text(
        json.dumps(["Baked Old Corp", "DE00 BAKED"]))
    fresh = [{"input": "EXAMPLE DOC A", "output": {"doc_type": "bank_letter",
              "fields": {"account_holder": "Fresh New GmbH"}}},
             {"input": "EXAMPLE DOC B", "output": {"doc_type": "bank_letter",
              "fields": {"account_holder": "Baked Old Corp",
                         "iban": "DE00 BAKED"}}}]
    monkeypatch.setattr(stage_b, "_load_fewshot", lambda dc, w8=False: fresh)
    monkeypatch.setattr(stage_b.mc, "resolve", lambda role: "mdmdoc-extract")
    raw = RawDoc(path="x.pdf", sha256="a" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = "some bank letter text"
    prompt = stage_b.build_prompt(raw)
    assert "Fresh New GmbH" in prompt          # fresh correction visible NOW
    assert "Baked Old Corp" not in prompt      # baked exemplar not duplicated


# --- D11d: pattern memory --------------------------------------------------------
def _label(sha, verdict="ACCEPT", confirmed=True):
    return {"ts": "2026-07-10T00:00:00Z", "doc_sha256": sha, "doc_class": "bank",
            "doc_type_gold": "bank_letter", "verdict_gold": verdict,
            "verdict_confirmed": confirmed, "scenarios": ["synth"],
            "fields_gold": {"account_holder": "X", "iban": {"present": True},
                            "signed": True}}


def _findings():
    return [Finding("BNK-021", "WARNING", "WARNING", "unsigned")]


def test_pattern_record_and_match(data_env):
    for i in range(3):
        patterns.record(_label(f"sha{i}"), _findings(), "WARNING")
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_holder": "Other Corp", "iban": "DE89370400440532013000",
                  "signed": True}
    assert patterns.match_count(ext, _findings()) == 3
    # different finding set -> no match
    assert patterns.match_count(ext, []) == 0
    # non-valid labels don't count
    patterns.record(_label("sha9", verdict="REJECT", confirmed=False),
                    _findings(), "WARNING")
    assert patterns.match_count(ext, _findings()) == 3


def test_pattern_rows_carry_no_values(data_env):
    patterns.record(_label("shaX"), _findings(), "WARNING")
    blob = (config.DATASET_DIR / "patterns.jsonl").read_text()
    assert "DE8937" not in blob and '"X"' not in blob.replace('"field_shape"', "")
    row = json.loads(blob.splitlines()[-1])
    assert row["field_shape"] == ["account_holder", "iban", "signed"]
    assert row["overridden"] == ["BNK-021"]


# --- D11b: note -> PENDING rule (additive only) ---------------------------------
CUR_YAML = """version: 1
doc_types: [bank_letter]
tables: {}
rules:
  - id: BNK-001
    name: existing
    applies_to: [bank_letter]
    when: {always: true}
    severity: NOTE
    verdict_effect: null
    message: "existing rule"
"""
ADDED_YAML = CUR_YAML + """
  - id: BNK-099
    name: operator_learned
    applies_to: [bank_letter]
    when: {field_missing: currency}
    severity: NOTE
    verdict_effect: null
    message: "currency missing"
"""


def test_only_adds_rules_gate():
    assert learning._only_adds_rules(CUR_YAML, ADDED_YAML) is True
    assert learning._only_adds_rules(CUR_YAML, CUR_YAML) is False        # nothing new
    modified = ADDED_YAML.replace('message: "existing rule"', 'message: "CHANGED"')
    assert learning._only_adds_rules(CUR_YAML, modified) is False        # edit blocked
    removed = ADDED_YAML.replace("  - id: BNK-001\n", "  - id: BNK-101\n")
    assert learning._only_adds_rules(CUR_YAML, removed) is False         # removal blocked


def test_note_to_rule_applies_additive_as_pending(data_env, monkeypatch):
    (config.RULES_DIR / "banking.yaml").write_text(CUR_YAML)
    prop = {"kind": "rule", "doc_class": "bank", "run_id": "r1", "rule_id": "BNK-099",
            "rationale": "x", "validation": [], "applicable": True,
            "current_yaml": CUR_YAML, "proposed_yaml": ADDED_YAML}
    monkeypatch.setattr(learning.rule_propose, "propose", lambda rid, note: prop)
    out = learning.note_to_rule("r1", "flag documents without a currency",
                                log=lambda *_: None)
    assert out == {"applied_pending": "BNK-099"}
    saved = (config.RULES_DIR / "banking.yaml").read_text()
    assert "BNK-099" in saved
    assert "source: operator" in saved and "tier: learned" in saved
    # the new rule is UNAPPROVED -> the gate keeps it from firing
    from mdmdoc import rule_approvals
    from mdmdoc.rules.engine import load_rules
    rule = next(r for r in load_rules("bank")["rules"] if r["id"] == "BNK-099")
    assert rule_approvals.status(rule_approvals.load(), "bank", rule) \
        == rule_approvals.PENDING


def test_note_to_rule_queues_modifications(data_env, monkeypatch):
    (config.RULES_DIR / "banking.yaml").write_text(CUR_YAML)
    modified = ADDED_YAML.replace('message: "existing rule"', 'message: "CHANGED"')
    prop = {"kind": "rule", "doc_class": "bank", "run_id": "r1", "rule_id": "BNK-001",
            "rationale": "x", "validation": [], "applicable": True,
            "current_yaml": CUR_YAML, "proposed_yaml": modified}
    monkeypatch.setattr(learning.rule_propose, "propose", lambda rid, note: prop)
    out = learning.note_to_rule("r1", "change the existing rule", log=lambda *_: None)
    assert out == {"queued": "modifies-existing"}
    assert (config.RULES_DIR / "banking.yaml").read_text() == CUR_YAML   # untouched
    rows = learning.load_proposals()
    assert rows and rows[-1]["reason_queued"] == "modifies existing rules"


def test_note_to_rule_queues_needs_code(data_env, monkeypatch):
    prop = {"kind": "needs_code", "doc_class": "bank", "run_id": "r1",
            "rationale": "needs a predicate"}
    monkeypatch.setattr(learning.rule_propose, "propose", lambda rid, note: prop)
    out = learning.note_to_rule("r1", "some structural idea", log=lambda *_: None)
    assert out == {"queued": "needs_code"}
    assert learning.load_proposals()[-1]["kind"] == "needs_code"


# --- D11e: ratings ---------------------------------------------------------------
def test_ratings_ledger_latest_wins(data_env):
    ratings.record("run1", "down")
    ratings.record("run1", "up")
    ratings.record("run2", "down")
    assert ratings.of("run1") == "up"
    assert ratings.of("run2") == "down"
    blob = (config.DATASET_DIR / "ratings.jsonl").read_text()
    assert blob.count("\n") == 3


# --- D11f: error_source routed into scenario tags --------------------------------
def test_error_source_becomes_scenario_tag():
    from mdmdoc import scenarios
    tags = scenarios.normalize_tags(["synth", "err_rule_wrong"])
    assert "err_rule_wrong" in tags
