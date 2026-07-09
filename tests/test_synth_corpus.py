"""M6/П1: the synthetic eval stratum — PII-free generated corpus with known
ground truth. Determinism, fake-identity safety, leak-gate compliance, honest
end-to-end deterministic runs, and strict stream separation from the headline
real-corpus artifacts."""
import json
from pathlib import Path

import pytest

from mdmdoc import config, synth
from mdmdoc.fields import iban_mod97_ok

ROOT = Path(__file__).resolve().parents[1]


# --- identity fakers ----------------------------------------------------------
def test_synth_iban_mod97_valid_and_invalid():
    import random
    rng = random.Random(7)
    for cc in ("DE", "GB", "NL", "ES", "CZ", "FR"):
        iban = synth.synth_iban(cc, rng)
        assert iban_mod97_ok(iban), f"{cc} fake IBAN must be checksum-valid"
        assert not iban_mod97_ok(synth.break_checksum(iban))


def test_fake_tin_ranges_clearly_fake():
    import random
    rng = random.Random(7)
    assert synth.synth_ein(rng).startswith("00-")     # never-assigned EIN prefix
    assert synth.synth_ssn(rng).startswith("000-")    # invalid SSN area


def test_generator_deterministic_same_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    synth.generate(seed=123, out_dir=a)
    synth.generate(seed=123, out_dir=b)
    assert (a / "labels.jsonl").read_text() == (b / "labels.jsonl").read_text()
    assert (a / "MANIFEST.json").read_text() == (b / "MANIFEST.json").read_text()


# --- the COMMITTED corpus -------------------------------------------------------
def _committed_labels() -> list[dict]:
    p = ROOT / "eval" / "synthetic" / "labels.jsonl"
    assert p.exists(), "committed synthetic corpus missing — run `mdmdoc synth-gen`"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_committed_labels_pass_leak_gate():
    from mdmdoc.privacy import assert_no_leak
    labels = _committed_labels()
    assert len(labels) >= 40
    fakes = [it["fake"] for lab in labels for it in lab.get("sensitive_map", [])]
    assert_no_leak((ROOT / "eval" / "synthetic" / "labels.jsonl").read_text(encoding="utf-8"),
                   [], allowed_fakes=fakes, policy="strict")


def test_committed_verdicts_match_current_rules():
    """Rule-drift detector without regeneration: recompute the truth-fold from
    each label's truth fields and compare to the committed verdict_gold."""
    from mdmdoc.rules.engine import run_rules
    from mdmdoc.verdict import decide
    from mdmdoc.fields import Extraction
    checked = 0
    for lab in _committed_labels():
        gold_fields = lab["fields_gold"]
        # reconstruct a truth extraction from the masked schema: only fields
        # whose full value survived (strings/bools) can be replayed; id dicts
        # are replayed via their sensitive_map fakes
        fields = {}
        for k, v in gold_fields.items():
            if k == "tin" and isinstance(v, dict):
                fields["tin_type"] = v.get("type", "")     # type lives in the dict
                continue
            fields[k] = v if not isinstance(v, dict) else ""
        for it in lab.get("sensitive_map", []):
            key = {"iban": "iban", "account_number": "account_number",
                   "tin": "tin_raw"}.get(it["kind"])
            if key and not fields.get(key):
                fields[key] = it["fake"]
        ext = Extraction(doc_class=lab["doc_class"], doc_type=lab["doc_type_gold"])
        ext.fields = fields
        got = decide(run_rules(ext, enforce_approvals=False))
        assert got == lab["verdict_gold"], \
            f"{lab['doc_path']}: rules now say {got}, label says {lab['verdict_gold']} " \
            "— rules changed; re-run `mdmdoc synth-gen` and review the diff"
        checked += 1
    assert checked >= 40


def test_scenario_matrix_covers_dimensions():
    m = json.loads((ROOT / "eval" / "synthetic" / "MANIFEST.json").read_text())
    matrix = m["matrix"]
    for tag in ("synth_bank_letter", "synth_bank_statement", "synth_invoice",
                "synth_payment_instructions", "synth_iban_invalid",
                "synth_no_holder", "synth_sig_unsigned", "synth_w9",
                "synth_w9_boxed", "synth_lang_de", "synth_lang_es"):
        assert matrix.get(tag, 0) >= 1, f"dimension {tag} not covered"


# --- end-to-end through the REAL pipeline (offline) -----------------------------
def _redirect_state(monkeypatch, tmp_path):
    import mdmdoc.runstore as rs
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "lora")
    # FEWSHOT_DIR must be redirected too: the leak sweep scans it, and the
    # repo's committed few-shot fakes are only excused by the real labels
    monkeypatch.setattr(config, "FEWSHOT_DIR", tmp_path / "fewshot")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(rs, "_LAST", tmp_path / "runs" / ".last")


@pytest.mark.parametrize("doc_idx", [0, 1, 2])
def test_synthetic_end_to_end_deterministic_run_check(monkeypatch, tmp_path, doc_idx):
    """Representative committed docs through the REAL run_check offline: the
    verdict must equal the recorded det_expected (CI regression lock)."""
    from mdmdoc.pipeline import run_check
    _redirect_state(monkeypatch, tmp_path)
    labels = _committed_labels()
    picks = [labels[0],                                          # esig letter
             next(l for l in labels if "synth_iban_invalid" in l["scenarios"]),
             next(l for l in labels if l["doc_class"] == "w9")]
    lab = picks[doc_idx]
    pdf = ROOT / "eval" / "synthetic" / "docs" / lab["doc_path"]
    res = run_check(pdf, lab["doc_class"], engine="deterministic",
                    apply_precedent=False, web_evidence=False,
                    enforce_approvals=False)
    assert res.verdict == lab["det_expected"]["verdict"], lab["doc_path"]
    assert res.pub["doc_type"] == lab["det_expected"]["doc_type"]


def test_synthetic_eval_writes_separate_stream(monkeypatch, tmp_path):
    """--dataset synthetic must never touch the headline artifacts."""
    from types import SimpleNamespace

    from mdmdoc import evalrun

    _redirect_state(monkeypatch, tmp_path)
    synth_dir = tmp_path / "eval" / "synthetic"
    monkeypatch.setattr(config, "SYNTH_DIR", synth_dir)
    monkeypatch.setattr(config, "SYNTH_DOCS_DIR", synth_dir / "docs")
    monkeypatch.setattr(config, "SYNTH_LABELS_PATH", synth_dir / "labels.jsonl")
    (synth_dir / "docs").mkdir(parents=True)
    (synth_dir / "docs" / "x.pdf").write_bytes(b"x")
    (synth_dir / "labels.jsonl").write_text(json.dumps(
        {"doc_path": "x.pdf", "doc_class": "bank", "doc_type_gold": "bank_letter",
         "verdict_gold": "ACCEPT", "fields_gold": {}, "scenarios": [],
         "sensitive_map": [], "source": "synthetic"}) + "\n")

    monkeypatch.setattr(evalrun, "run_check", lambda *a, **k: SimpleNamespace(
        pub={"doc_type": "bank_letter", "json_valid_first_try": True, "fields": {},
             "tier": "fast"}, verdict="ACCEPT", run_id="cafe" * 4, findings=[]))
    rc = evalrun.run_eval(record=True, dataset="synthetic")
    assert rc == 0
    ev = tmp_path / "eval"
    assert (ev / "synthetic_results.json").exists()
    assert (ev / "synthetic_report.md").exists()
    assert (ev / "synthetic_history.jsonl").exists()
    assert not (ev / "last_results.json").exists()      # headline untouched
    assert not (ev / "report.md").exists()
    assert not (ev / "history.jsonl").exists()


def test_load_labels_never_sees_synthetic():
    """few-shot / LoRA / precedents read load_labels() — synthetic rows must be
    structurally invisible there."""
    from mdmdoc.dataset import load_labels, load_synth_labels
    real = load_labels()
    assert all(l.get("source") != "synthetic" for l in real)
    if config.SYNTH_LABELS_PATH.exists():
        assert all(l.get("source") == "synthetic" for l in load_synth_labels())
