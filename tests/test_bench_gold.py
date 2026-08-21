"""Gold pipeline without the network: the SDK session is monkeypatched."""
import asyncio
import json

import fitz
import pytest

from mdmdoc import config
from mdmdoc.bench import gold, manifest


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BENCH_DIR", tmp_path / "bench")
    from mdmdoc import ocr
    monkeypatch.setattr(ocr, "HAVE_TESSERACT", False)
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "Account 4830 2291 0077", fontsize=14)
    pdf = tmp_path / "g.pdf"
    doc.save(pdf)
    doc.close()
    manifest.add([pdf], tags=["core"])
    return manifest.load("all")[0]


PASS1 = {"doc_type": "bank_letter", "doc_type_free": "bank letter", "doc_type_confidence": 0.9,
         "languages": ["en"], "text": "Account 4830 2291 0O77", "fields": [
             {"label": "Account", "value": "4830 2291 0O77", "handwritten": False}],
         "unreadable_count": 0, "handwriting_present": False, "tables_present": False, "notes": ""}
PASS2 = dict(PASS1, text="Account 4830 2291 0077",
             fields=[{"label": "Account", "value": "4830 2291 0077", "handwritten": False}],
             corrections=[{"before": "0O77", "after": "0077", "reason": "digit zero, not letter O"}])


def _fake_session_factory(calls):
    async def fake(model, system_prompt, user_prompt, images, schema, timeout, max_turns=20):
        calls.append({"model": model, "images": [str(p) for p in images], "schema": schema,
                      "system": system_prompt[:40], "user": user_prompt})
        assert len(images) >= 2                      # full page + tiles
        if "corrections" in schema["properties"]:
            return PASS2, {"cost_usd": 0.4, "duration_s": 3.0, "num_turns": 6, "session_id": "s2", "usage": {}}
        return PASS1, {"cost_usd": 0.5, "duration_s": 4.0, "num_turns": 7, "session_id": "s1", "usage": {}}
    return fake


def test_two_pass_gold_and_cache(bench, monkeypatch):
    calls = []
    monkeypatch.setattr(gold, "_session", _fake_session_factory(calls))
    rec = asyncio.run(gold.gold_page(bench, 0, model="claude-test", timeout=60))
    assert rec["status"] == "verified"
    assert rec["final"]["text"] == "Account 4830 2291 0077"
    assert rec["corrections"][0]["after"] == "0077"
    assert rec["disagreement_cer"] > 0
    assert rec["cost_usd"] == pytest.approx(0.9)
    assert len(calls) == 2 and "4830 2291 0O77" in calls[1]["user"]     # pass 2 sees pass 1
    p = gold.gold_path(bench.doc_id, 0)
    assert p.exists() and (gold.gold_dir(bench.doc_id) / "gold.md").exists()
    # cached: no new sessions
    rec2 = asyncio.run(gold.gold_page(bench, 0, model="claude-test", timeout=60))
    assert rec2.get("_cached") and len(calls) == 2
    # different model → regenerate
    asyncio.run(gold.gold_page(bench, 0, model="claude-other", timeout=60))
    assert len(calls) == 4
    # force → regenerate even for the same model
    asyncio.run(gold.gold_page(bench, 0, model="claude-other", timeout=60, force=True))
    assert len(calls) == 6


def test_single_pass_and_error_record(bench, monkeypatch):
    calls = []
    monkeypatch.setattr(gold, "_session", _fake_session_factory(calls))
    rec = asyncio.run(gold.gold_page(bench, 0, model="m", timeout=60, single_pass=True))
    assert rec["status"] == "auto" and rec["final"]["text"].endswith("0O77") and len(calls) == 1

    async def boom(*a, **k):
        raise gold.GoldError("session error: something")
    monkeypatch.setattr(gold, "_session", boom)
    rec = asyncio.run(gold.gold_page(bench, 0, model="m2", timeout=60))
    assert rec["status"] == "error" and "GoldError" in rec["error"]

    async def auth(*a, **k):
        raise gold.GoldAuthError("not logged in")
    monkeypatch.setattr(gold, "_session", auth)
    with pytest.raises(gold.GoldAuthError):
        asyncio.run(gold.gold_page(bench, 0, model="m3", timeout=60))


def test_human_checked_is_sticky_and_review_html(bench, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(gold, "_session", _fake_session_factory(calls))
    asyncio.run(gold.gold_page(bench, 0, model="m", timeout=60))
    txt = bench.abs_path.parent / "fixed.txt"
    txt.write_text("Account 4830 2291 0077 (human)", encoding="utf-8")

    class A:  # argparse stand-in
        doc_id = bench.doc_id
        page = 0
        text = str(txt)
    assert gold.cli_fix(A) == 0
    rec = json.loads(gold.gold_path(bench.doc_id, 0).read_text())
    assert rec["status"] == "human_checked" and rec["final"]["text"].endswith("(human)")
    # a later gold run must not overwrite a human-checked page
    rec2 = asyncio.run(gold.gold_page(bench, 0, model="other-model", timeout=60))
    assert rec2["status"] == "human_checked" and rec2.get("_cached")
    html = gold.review_html(bench, 0, rec)
    assert "human_checked" in html and "gold-accept" in html and "(human)" in html

    class R:
        filter = "all"
        n = 5
        all = True
    assert gold.cli_review(R) == 0
    assert (config.BENCH_DIR / "gold" / "review" / "index.html").exists()


def test_looks_logged_out_and_schema_shape():
    assert gold.looks_logged_out("Error: Not logged in. Please run /login")
    assert not gold.looks_logged_out("timeout")
    assert "corrections" in gold.VERIFY_SCHEMA["properties"]
    assert "corrections" not in gold.GOLD_SCHEMA["properties"]
    assert set(gold.GOLD_SCHEMA["required"]) >= {"text", "fields", "doc_type", "languages"}
