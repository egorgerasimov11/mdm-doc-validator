"""Verdict-safety wave (audit-wave): fail-closed behaviors added after the
2026-07 audit — concurrent label writes, SAP-compare crash containment,
precedent relaxation gating."""
import json
import threading

import pytest

from mdmdoc import config, dataset


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


def _run_id_of(path) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _write_label(tmp_path, run_id: str, **extra):
    lab = {"doc_sha256": run_id, "confirmed": True,
           "doc_type_gold": "bank_letter", **extra}
    (tmp_path / "dataset").mkdir(parents=True, exist_ok=True)
    config.LABELS_PATH.write_text(json.dumps(lab) + "\n", encoding="utf-8")


# --- C10: a SAP-compare bug must fail closed, never abort the run ------------
def test_sap_compare_crash_fails_closed(monkeypatch, tmp_path, tiny_pdf):
    import openpyxl

    import mdmdoc.sap_tables as st
    from mdmdoc.pipeline import run_check

    _redirect_state(monkeypatch, tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PARTNER", "BKVID", "BANKS", "BANKL", "BANKN", "BKONT", "BKREF",
               "KOINH", "IBAN"])
    ws.append(["50000111", "0001", "DE", "37040044", "0532013000", "", "", "Acme", ""])
    xlsx = tmp_path / "but0bk.xlsx"
    wb.save(xlsx)

    monkeypatch.setattr(st, "select_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = run_check(tiny_pdf, "bank", engine="deterministic", apply_precedent=False,
                    web_evidence=False, enforce_approvals=False, sap_image=xlsx)
    assert any(f.rule_id == "SAP-014" for f in res.findings)
    assert res.verdict in ("NEED_MANUAL_REVIEW", "REJECT")
    assert any("NOT verified against SAP" in w for w in res.pub.get("warnings", []))


def test_select_row_blank_partners_returns_best_row():
    from mdmdoc.fields import Extraction
    from mdmdoc.sap_tables import select_row
    rows = [{"PARTNER": "", "BKVID": "0001", "BANKS": "IT", "BANKL": "0542811101",
             "BANKN": "000000123456", "KOINH": "ACME",
             "IBAN": "IT60X0542811101000000123456"}]
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"iban": "IT60X0542811101000000123456"}
    row, findings, partners = select_row(rows, ext, "")
    assert row is not None                       # no IndexError (old bug)
    assert partners == []
    assert any(f.rule_id == "SAP-012" and "no Partner values" in f.message
               for f in findings)


# --- C11: precedent may tighten freely; relaxing needs explicit confirmation --
def test_precedent_relax_blocked_without_confirmation(monkeypatch, tmp_path, tiny_pdf):
    from mdmdoc.pipeline import run_check
    _redirect_state(monkeypatch, tmp_path)
    # deterministic run on this letter yields NMR (holder not extracted without
    # an LLM) — the stored ACCEPT precedent would RELAX it
    _write_label(tmp_path, _run_id_of(tiny_pdf), verdict_gold="ACCEPT")
    res = run_check(tiny_pdf, "bank", engine="deterministic", apply_precedent=True,
                    web_evidence=False, enforce_approvals=False)
    assert res.verdict != "ACCEPT"                                  # machine kept
    assert any(f.rule_id == "OPERATOR-2" for f in res.findings)     # explains why
    assert res.pub["operator_precedent"]["relax_blocked"] is True


def test_precedent_relax_applies_with_confirmation(monkeypatch, tmp_path, tiny_pdf):
    from mdmdoc.pipeline import run_check
    _redirect_state(monkeypatch, tmp_path)
    _write_label(tmp_path, _run_id_of(tiny_pdf), verdict_gold="ACCEPT",
                 verdict_confirmed=True)
    res = run_check(tiny_pdf, "bank", engine="deterministic", apply_precedent=True,
                    web_evidence=False, enforce_approvals=False)
    assert res.verdict == "ACCEPT"
    op1 = [f for f in res.findings if f.rule_id == "OPERATOR-1"]
    assert op1 and op1[0].severity == "WARNING"          # relaxation is VISIBLE
    assert "RELAXED" in op1[0].message


def test_precedent_tighten_applies_without_confirmation(monkeypatch, tmp_path, tiny_pdf):
    from mdmdoc.pipeline import run_check
    _redirect_state(monkeypatch, tmp_path)
    _write_label(tmp_path, _run_id_of(tiny_pdf), verdict_gold="REJECT")
    res = run_check(tiny_pdf, "bank", engine="deterministic", apply_precedent=True,
                    web_evidence=False, enforce_approvals=False)
    assert res.verdict == "REJECT"                       # tightening needs no flag
    op1 = [f for f in res.findings if f.rule_id == "OPERATOR-1"]
    assert op1 and op1[0].severity == "NOTE"


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
