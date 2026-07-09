"""Universal analysis engine: a persisted mode (auto / deterministic / llm-first
/ dual), graceful degradation when the LLM host is down (a check NEVER fails
because Ollama is unreachable), and the dual-mode engine-comparison artifact."""
import json

import pytest

from mdmdoc import config
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc
from mdmdoc.stage_b import _engine_compare, extract


# --- mode resolution ------------------------------------------------------
def test_engine_mode_default_auto(monkeypatch, tmp_path):
    monkeypatch.delenv("MDMDOC_ENGINE", raising=False)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    assert config.engine_mode() == "auto"


def test_engine_mode_settings_file(monkeypatch, tmp_path):
    monkeypatch.delenv("MDMDOC_ENGINE", raising=False)
    p = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    config.save_setting("engine", "dual")
    assert json.loads(p.read_text())["engine"] == "dual"
    assert config.engine_mode() == "dual"


def test_engine_mode_env_beats_settings(monkeypatch, tmp_path):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    config.save_setting("engine", "dual")
    monkeypatch.setenv("MDMDOC_ENGINE", "deterministic")
    assert config.engine_mode() == "deterministic"
    monkeypatch.setenv("MDMDOC_ENGINE", "bogus")   # unknown env value is ignored
    assert config.engine_mode() == "dual"


# --- deterministic engine: no LLM call, fields from patterns ---------------
def test_deterministic_engine_never_calls_the_model(monkeypatch):
    import mdmdoc.stage_b as sb

    def boom(*a, **k):
        raise AssertionError("deterministic engine must not call the LLM")

    monkeypatch.setattr(sb, "_run_model", boom)
    monkeypatch.setattr(sb.mc, "strong_distinct", lambda: True)
    raw = RawDoc(path="/x/letter.pdf", sha256="a" * 64, ext=".pdf", doc_class="bank")
    raw.raw_text = "Bank letter. IBAN DE89 3704 0044 0532 0130 00 Account holder Acme"
    raw.regex_candidates = {"iban": "DE89370400440532013000"}
    ext = extract(raw, engine="deterministic")
    assert ext.engine == "deterministic"
    assert ext.tier == "fast" and not ext.escalated_because
    assert ext.fields.get("iban") == "DE89370400440532013000"  # filled by crosscheck
    assert any("deterministic engine" in w for w in ext.warnings)
    assert ext.model_id == "(deterministic — no LLM)"


# --- dual engine: structured comparison, masked ----------------------------
def test_engine_compare_agree_disagree_only():
    raw = RawDoc(path="/x/d.pdf", sha256="b" * 64, ext=".pdf", doc_class="bank")
    raw.regex_candidates = {"iban": "DE89370400440532013000",
                            "account_number": "12345678",
                            "swift_bic": "COBADEFFXXX"}
    llm = {"iban": "DE89 3704 0044 0532 0130 00",   # same, spaced -> agree
           "account_number": "99999999",            # differs -> disagree
           "swift_bic": ""}                          # only deterministic
    rows = {r["field"]: r for r in _engine_compare(llm, raw, "bank")}
    assert rows["iban"]["agree"] is True
    assert rows["account_number"]["agree"] is False
    assert rows["swift_bic"]["agree"] is None and rows["swift_bic"]["only"] == "deterministic"
    # values ship masked — full IBAN/account must not appear
    blob = json.dumps(list(rows.values()))
    assert "DE89370400440532013000" not in blob
    assert "99999999" not in blob and "12345678" not in blob


def test_engine_compare_zero_padded_account_agrees():
    raw = RawDoc(path="/x/d.pdf", sha256="c" * 64, ext=".pdf", doc_class="bank")
    raw.regex_candidates = {"account_number": "0001234567"}
    rows = _engine_compare({"account_number": "1234567"}, raw, "bank")
    assert rows[0]["agree"] is True


# --- pipeline: LLM down -> auto-degrade, run completes ----------------------
@pytest.fixture()
def tiny_pdf(tmp_path):
    import fitz
    p = tmp_path / "letter.pdf"
    d = fitz.open()
    page = d.new_page()
    page.insert_text((72, 100), "Bank confirmation letter")
    page.insert_text((72, 130), "Account holder: Acme Industries GmbH")
    page.insert_text((72, 160), "IBAN: DE89 3704 0044 0532 0130 00")
    page.insert_text((72, 190), "Bank: Commerzbank AG, signed by officer")
    d.save(p)
    d.close()
    return p


def _redirect_state(monkeypatch, tmp_path):
    import mdmdoc.runstore as rs
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path / "dataset")
    monkeypatch.setattr(config, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "LORA_DIR", tmp_path / "lora")
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "dataset" / "labels.jsonl")
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(rs, "_LAST", tmp_path / "runs" / ".last")


def test_run_check_degrades_when_llm_down(monkeypatch, tmp_path, tiny_pdf):
    import mdmdoc.model_client as mc
    import mdmdoc.stage_b as sb
    from mdmdoc.pipeline import run_check
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.delenv("MDMDOC_ENGINE", raising=False)

    def down(*a, **k):
        raise mc.OllamaUnavailable("host down (test)")

    monkeypatch.setattr(mc, "preflight", down)
    monkeypatch.setattr(sb, "_run_model",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM")))
    res = run_check(tiny_pdf, "bank", engine="auto", apply_precedent=False,
                    web_evidence=False, enforce_approvals=False)
    assert any(f.rule_id == "ENGINE-001" for f in res.findings)
    assert res.pub["engine"] == "deterministic"
    assert res.verdict in ("ACCEPT", "WARNING", "NEED_MANUAL_REVIEW", "REJECT")
    meta = json.loads((config.RUNS_DIR / res.run_id / "meta.json").read_text())
    assert meta["engine_requested"] == "auto"
    assert meta["engine_effective"] == "deterministic"
    assert meta["llm_ok"] is False


def test_run_check_deterministic_skips_preflight(monkeypatch, tmp_path, tiny_pdf):
    import mdmdoc.model_client as mc
    import mdmdoc.stage_b as sb
    from mdmdoc.pipeline import run_check
    _redirect_state(monkeypatch, tmp_path)

    monkeypatch.setattr(mc, "preflight",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("preflight must be skipped")))
    monkeypatch.setattr(sb, "_run_model",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM")))
    res = run_check(tiny_pdf, "bank", engine="deterministic", apply_precedent=False,
                    web_evidence=False, enforce_approvals=False)
    # requested deterministic on purpose -> no ENGINE-001 degradation warning
    assert not any(f.rule_id == "ENGINE-001" for f in res.findings)
    assert res.pub["engine"] == "deterministic"
    assert res.pub["fields"].get("iban", {}).get("present") is True
