"""P2 (quality wave): the text-layer sanity gate. A custom font without a
ToUnicode CMap extracts as control-char soup that still yields enough
word-shaped tokens to pass the has_text_layer gate — the document must be
rerouted to the scan branch instead of feeding garbage to the extractor.
Thresholds calibrated against the real garbage packet (True) and every doc in
the local corpus + eval/synthetic (all False)."""
from pathlib import Path

import pytest

from mdmdoc.ocr import text_layer_garbage

CITIZENS_STYLE = """To whom it may concern,
We hereby confirm that Vela Industries LLC is known to us and has accounts in
good standing at First Testbank. The account details are as provided above and
can be used for both incoming wires and ACH transfers.
Sincerely,
Jordan Q. Sample
Vice President | Relationship Manager
""" * 3


def test_control_soup_is_garbage():
    texts = ["\x00\x01\x02\x03" * 100 + "wire transfer account bank " * 4]
    assert text_layer_garbage(texts) is True          # ratio branch


def test_scattered_fragments_are_garbage():
    # single latin letters separated by control/PUA chars — few 2+-letter runs
    frag = ("a\x01bc\x02d" * 80) + " account " * 3
    assert text_layer_garbage([frag]) is True


def test_normal_letter_not_garbage():
    assert text_layer_garbage([CITIZENS_STYLE]) is False


def test_cjk_with_latin_labels_not_garbage():
    body = "银行账户确认书。" * 200 + " Account Number Bank of Test branch office"
    assert text_layer_garbage([body]) is False        # CJK not in denominator


def test_cyrillic_not_garbage():
    body = "Подтверждение банковских реквизитов поставщика. " * 40 \
        + " IBAN account bank confirmation letter details"
    assert text_layer_garbage([body]) is False


def test_empty_and_none_safe():
    assert text_layer_garbage([]) is False
    assert text_layer_garbage(["", "   "]) is False


def test_garbage_pdf_reroutes_to_scan_branch(tmp_path, monkeypatch):
    """e2e: a PDF whose extracted layer is soup goes down the SCAN branch."""
    import fitz

    from mdmdoc import stage_a
    p = tmp_path / "packet.pdf"
    d = fitz.open()
    for _ in range(3):
        d.new_page()
    d.save(p)
    d.close()

    garbage = ["\x00\x01\x02\x03" * 200 + "account bank wire routing " * 3] * 3
    monkeypatch.setattr(stage_a, "_pdf_page_texts", lambda path, cap: (garbage, 3))
    survey_calls = []

    def fake_survey(path, render_dir, doc_class):
        survey_calls.append(path)
        return [(5, 2, "Account Number: 000111222333 Bank of Testville "
                       "confirmation letter for the vendor", 0),
                (1, 0, "cover sheet", 0)]

    monkeypatch.setattr(stage_a, "_survey_scanned_pdf", fake_survey)

    def fake_deep(path, picks, render_dir, raw, use_vision):
        raw.tesseract_text = "\n".join(t for _, _, t, _ in picks)

    monkeypatch.setattr(stage_a, "_deep_read_pages", fake_deep)
    raw = stage_a.perceive(p, "bank", tmp_path, use_vision=False)
    assert survey_calls, "survey (scan branch) must run"
    assert raw.has_text_layer is False
    assert raw.text_layer_garbage is True
    assert any("unreadable" in w for w in raw.warnings)
    assert 2 in raw.pages_used                       # best survey page selected
