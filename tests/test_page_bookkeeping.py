"""P3 (quality wave): per-page bookkeeping at zero model cost — W-9/W-8 page
markers for bank packets and cheap survey-text retention for the evidence
ladder / signature-page hints."""
import fitz
import pytest

from mdmdoc import stage_a
from mdmdoc.fields import page_markers
from mdmdoc.stage_a import RawDoc, _collect_markers


def test_page_markers_w9_form():
    assert page_markers("Form W-9\nRequest for Taxpayer Identification Number "
                        "and Certification")["w9_form"] is True
    assert page_markers("W-8BEN-E Certificate of Foreign Status")["w9_form"] is True
    assert page_markers("Bank confirmation letter. We confirm that Vela s.r.o. "
                        "holds an account with us.")["w9_form"] is False


def test_collect_markers_records_w9_pages():
    raw = RawDoc(path="/x.pdf", sha256="a" * 16, ext=".pdf", doc_class="bank")
    _collect_markers(raw, [
        (0, "Supplier banking information sheet, account details"),
        (1, "Form W-9 Request for Taxpayer Identification Number and Certification"),
    ], "bank")
    assert raw.w9_pages == [1]


def test_collect_markers_w9_class_untouched():
    raw = RawDoc(path="/x.pdf", sha256="a" * 16, ext=".pdf", doc_class="w9")
    _collect_markers(raw, [(0, "Form W-9 blah")], "w9")
    assert raw.w9_pages == []                        # bank-packet concept only


def test_survey_texts_retained_text_branch(tmp_path):
    p = tmp_path / "t.pdf"
    d = fitz.open()
    for i in range(3):
        pg = d.new_page()
        pg.insert_text((72, 100), f"Bank confirmation letter page {i} with the "
                                  f"account holder and enough words to pass the gate")
    d.save(p)
    d.close()
    raw = stage_a.perceive(p, "bank", tmp_path, use_vision=False)
    assert raw.has_text_layer is True
    assert set(raw.survey_texts) == {0, 1, 2}        # ALL pages retained
    assert "page 1" in raw.survey_texts[1]


def test_survey_texts_retained_scan_branch(tmp_path, monkeypatch):
    p = tmp_path / "s.pdf"
    d = fitz.open()
    d.new_page()
    d.new_page()
    d.save(p)
    d.close()
    monkeypatch.setattr(stage_a, "_survey_scanned_pdf",
                        lambda path, rd, dc: [(3, 0, "bank letter text", 0),
                                              (1, 1, "second page", 0)])
    monkeypatch.setattr(stage_a, "_deep_read_pages",
                        lambda path, picks, rd, raw, uv: None)
    raw = stage_a.perceive(p, "bank", tmp_path, use_vision=False)
    assert raw.survey_texts == {0: "bank letter text", 1: "second page"}
