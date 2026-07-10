"""P6: the evidence ladder — triggers only on critical gaps + promising unread
pages, is budget-bounded, never blanks a first-pass value. Clean docs pay zero."""
import time

import fitz
import pytest

from mdmdoc import config, ladder, stage_a, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

HOLDER_PAGE = ("Supplier banking sheet\nAccount holder: Fake Corp GmbH\n"
               "Beneficiary name: Fake Corp GmbH")
NOISE_PAGE = "General terms and conditions. Nothing of interest here."


@pytest.fixture()
def pdf3(tmp_path):
    """3-page text-layer PDF; the first pass 'read' only page 0."""
    p = tmp_path / "packet.pdf"
    d = fitz.open()
    for text in ("This letter is to confirm the account details below.",
                 NOISE_PAGE, HOLDER_PAGE):
        pg = d.new_page()
        pg.insert_text((72, 100), text)
    d.save(p)
    d.close()
    return p


def _raw(pdf3, pages_used=(0,)):
    r = RawDoc(path=str(pdf3), sha256="c" * 16, ext=".pdf", doc_class="bank")
    r.has_text_layer = True
    r.survey_texts = {0: "This letter is to confirm the account details below.",
                      1: NOISE_PAGE, 2: HOLDER_PAGE}
    r.pages_used = list(pages_used)
    r.page_texts = {i: r.survey_texts[i] for i in pages_used}
    r.raw_text = "\n".join(r.page_texts.values())
    return r


def _ext(reasons, **fields):
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"account_holder": "", "bank_name": "Fake Bank", **fields}
    e.escalated_because = list(reasons)
    return e


def _climb(pdf3, raw, ext, monkeypatch, ext2_fields=None, t0=None):
    calls = {"n": 0}

    def fake_extract(raw_, quality=False, policy="masked", engine="auto",
                     injected_llm=None):
        calls["n"] += 1
        e2 = Extraction(doc_class="bank", doc_type="bank_letter")
        e2.fields = dict(ext2_fields or {})
        return e2

    monkeypatch.setattr(stage_b, "extract", fake_extract)
    out, meta = ladder.climb(pdf3, raw, ext, pdf3.parent,
                             t0=t0 if t0 is not None else time.time(),
                             quality=False, policy="masked", engine="auto",
                             use_vision=False)
    return out, meta, calls["n"]


def test_clean_doc_pays_zero(pdf3, monkeypatch):
    ext = _ext([], account_holder="Fake Corp GmbH")
    out, meta, n = _climb(pdf3, _raw(pdf3), ext, monkeypatch)
    assert meta["used"] is False and n == 0 and out is ext


def test_kill_switch(pdf3, monkeypatch):
    monkeypatch.setenv("MDMDOC_LADDER", "0")
    out, meta, n = _climb(pdf3, _raw(pdf3), _ext(["bank-no-holder"]), monkeypatch)
    assert meta["used"] is False and n == 0


def test_time_budget_refuses(pdf3, monkeypatch):
    out, meta, n = _climb(pdf3, _raw(pdf3), _ext(["bank-no-holder"]), monkeypatch,
                          t0=time.time() - config.time_budget_s() - 1)
    assert meta["used"] is False and meta.get("skipped") == "time-budget" and n == 0


def test_climb_reads_promising_page_and_fills_gap(pdf3, monkeypatch):
    raw = _raw(pdf3)
    out, meta, n = _climb(pdf3, raw, _ext(["bank-no-holder"]), monkeypatch,
                          ext2_fields={"account_holder": "Fake Corp GmbH"})
    assert meta["used"] is True and n == 1
    assert meta["pages"] == [3]                    # holder page, 1-based
    assert out.fields["account_holder"] == "Fake Corp GmbH"
    assert 2 in raw.pages_used and "Fake Corp GmbH" in raw.raw_text
    assert any("evidence ladder" in w for w in out.warnings)


def test_gap_bonus_skips_noise_pages(pdf3, monkeypatch):
    raw = _raw(pdf3, pages_used=(0, 2))            # only NOISE unread
    out, meta, n = _climb(pdf3, raw, _ext(["bank-no-holder"]), monkeypatch)
    assert meta["used"] is False and n == 0        # no page with bonus >= 1


def test_never_blanks_first_pass_value(pdf3, monkeypatch):
    out, meta, _ = _climb(pdf3, _raw(pdf3), _ext(["bank-no-holder"]), monkeypatch,
                          ext2_fields={"account_holder": "Fake Corp GmbH",
                                       "bank_name": ""})
    assert out.fields["bank_name"] == "Fake Bank"  # re-read may not blank


def test_non_critical_reasons_do_not_trigger(pdf3, monkeypatch):
    out, meta, n = _climb(pdf3, _raw(pdf3), _ext(["json-retry"]), monkeypatch)
    assert meta["used"] is False and n == 0


def test_scan_branch_ocr_and_vision_cap(pdf3, monkeypatch, tmp_path):
    raw = _raw(pdf3)
    raw.has_text_layer = False                     # scan: render + OCR path
    monkeypatch.setattr(stage_a, "_quick_ocr", lambda p: HOLDER_PAGE)
    monkeypatch.setattr(stage_a, "_best_rotation", lambda p, t: (0, t))
    vision_calls = {"n": 0}

    def fake_vision(role, prompt, images, options=None):
        vision_calls["n"] += 1
        return "Account holder: Fake Corp GmbH"

    monkeypatch.setattr(stage_a.mc, "vision", fake_vision)
    monkeypatch.setattr(stage_a.mc, "unload", lambda role: None)
    stats = stage_a.deep_read_extra_pages(pdf3, raw, tmp_path, [2],
                                          use_vision=True, vision_cap=1)
    assert stats["pages"] == [2] and stats["vision_calls"] == 1
    assert vision_calls["n"] == 1
    assert raw.page_texts[2] == HOLDER_PAGE
    stats2 = stage_a.deep_read_extra_pages(pdf3, raw, tmp_path, [1],
                                           use_vision=True, vision_cap=0)
    assert stats2["vision_calls"] == 0 and vision_calls["n"] == 1
