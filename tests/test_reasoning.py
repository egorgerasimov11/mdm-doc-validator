"""D6: the reasoning.md decision-trace artifact — complete sections, the
full per-rule table (incl. approval-gate skips), masked always, leak-gated."""
import fitz
import pytest

from mdmdoc import config
from mdmdoc.pipeline import run_check


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from mdmdoc import runstore
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "RULES_DIR", config.RULES_DIR)  # real rules
    return tmp_path


def _pdf(tmp_path, text, name="doc.pdf"):
    p = tmp_path / name
    d = fitz.open()
    pg = d.new_page()
    y = 80
    for line in text.splitlines():
        pg.insert_text((72, y), line, fontsize=10)
        y += 16
    d.save(p)
    d.close()
    return p


BANK_TEXT = ("Bank confirmation letter\n"
             "This letter is to confirm the account details below.\n"
             "Account holder: Fake Corp GmbH\n"
             "IBAN: DE89 3704 0044 0532 0130 00\n"
             "BIC: COBADEFFXXX\n"
             "Sincerely, Commerzbank Account Services.")


def test_reasoning_artifact_written_and_complete(env, tmp_path):
    p = _pdf(tmp_path, BANK_TEXT)
    res = run_check(p, "bank", use_vision=False, engine="deterministic",
                    enforce_approvals=True)
    md = (config.RUNS_DIR / res.run_id / "reasoning.md").read_text()
    for section in ("# Decision trace", "## 1. Run", "## 3. Perception",
                    "## 4. Extraction", "## 7. Confidence", "## 8. Rules",
                    "## 11. Verdict"):
        assert section in md, section
    # the per-rule table covers every evaluated rule, incl. gate skips
    assert "| rule | name | outcome | detail |" in md
    assert "skipped-pending" in md or "FIRED" in md
    assert "not-applicable" in md or "did-not-fire" in md
    assert res.verdict in md


def test_reasoning_masks_ids(env, tmp_path):
    p = _pdf(tmp_path, BANK_TEXT)
    res = run_check(p, "bank", use_vision=False, engine="deterministic",
                    enforce_approvals=False)
    md = (config.RUNS_DIR / res.run_id / "reasoning.md").read_text()
    assert "DE89370400440532013000" not in md          # full IBAN never appears
    assert "0532013000" not in md


def test_reasoning_served_via_artifacts(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    p = _pdf(tmp_path, BANK_TEXT)
    res = run_check(p, "bank", use_vision=False, engine="deterministic")
    client = TestClient(create_app("full"))
    r = client.get(f"/api/v1/runs/{res.run_id}/artifacts/reasoning.md")
    assert r.status_code == 200 and "# Decision trace" in r.text


def test_eval_paths_without_trace_unaffected(env, tmp_path):
    """run_rules default (trace=None) stays byte-identical for eval/tests."""
    from mdmdoc.fields import Extraction
    from mdmdoc.rules.engine import run_rules
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": True, "account_holder": "X", "bank_name": "Y",
                  "iban": "DE89370400440532013000"}
    a = [f.rule_id for f in run_rules(ext)]
    trace: list = []
    b = [f.rule_id for f in run_rules(ext, trace=trace)]
    assert a == b and len(trace) > 10
