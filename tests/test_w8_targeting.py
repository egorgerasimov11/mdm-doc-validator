"""P4 (quality wave): W-8 required-page targeting + signature-page hints.
An 8-page W-8BEN-E's evidence lives on SPECIFIC pages (Part I up front,
chapter-4 Part in the middle, Part XXX certification at the end) — generic
keyword scoring picked pages blindly and the signature probe landed on the
wrong page while the real signature sat on the last one."""
import fitz
import pytest

from mdmdoc import stage_a
from mdmdoc.stage_a import RawDoc, _select_w8_pages, _signature_page, _w8_detected

# 8 synthetic survey pages modeled on a real W-8BEN-E shape (invented text)
W8_PAGES = [
    "Form W-8BEN-E Certificate of Foreign Status. Part I Identification of "
    "Beneficial Owner. Name of organization. Country of incorporation.",     # 0
    "Part II Disregarded entity information filler text",                    # 1
    "Part III Claim of Tax Treaty Benefits filler",                          # 2
    "instructions filler",                                                   # 3
    "more instructions filler",                                              # 4
    "Part XXI 501(c) Organization filler",                                   # 5
    "Part XXIII Publicly Traded NFFE. I certify that the stock is regularly "
    "traded on an established securities market.",                           # 6
    "Part XXX Certification. Sign Here. Signature of individual authorized "
    "to sign. Print Name. Date.",                                            # 7
]


def _scored(pages=W8_PAGES):
    return [(0, i, t, 0) for i, t in enumerate(pages)]


def test_select_w8_part1_cert_best():
    picks, cert = _select_w8_pages(_scored(), 3)
    idxs = [i for _, i, _, _ in picks]
    assert cert == 7                       # Part XXX wins the ladder
    assert idxs[0] == 0 and 7 in idxs      # Part I first, cert included
    assert len(idxs) == 3


def test_select_w8_cert_fallback_ladder():
    # no Part XXX -> Sign Here page wins
    pages = [p.replace("Part XXX", "Part Q") for p in W8_PAGES]
    _, cert = _select_w8_pages(_scored(pages), 3)
    assert cert == 7                       # still 7 via "Sign Here"
    # neither -> LAST 'Certification' page
    pages2 = [p.replace("Part XXX", "Part Q").replace("Sign Here", "___")
              for p in pages]
    _, cert = _select_w8_pages(_scored(pages2), 3)
    assert cert == 7                       # "Certification." on p7
    # none at all -> last page
    pages3 = ["filler text only"] * 5
    _, cert = _select_w8_pages(_scored(pages3), 3)
    assert cert == 4


def test_w8_detection_filename_and_text(tmp_path):
    from pathlib import Path
    assert _w8_detected(Path("/x/2026 05 w8ben.pdf"), ["no marks"], "w9")
    assert _w8_detected(Path("/x/form.pdf"),
                        ["Certificate of Foreign Status of Beneficial Owner"], "w9")
    assert not _w8_detected(Path("/x/form.pdf"), ["Form W-9 request"], "w9")
    assert not _w8_detected(Path("/x/w8ben.pdf"), ["anything"], "bank")


def test_signature_page_uses_w8_cert_page(tmp_path):
    p = tmp_path / "w8.pdf"
    d = fitz.open()
    d.new_page()
    d.save(p)
    d.close()
    raw = RawDoc(path=str(p), sha256="a" * 16, ext=".pdf", doc_class="w9",
                 w8_cert_page=7, pages_used=[0, 7, 1])
    assert _signature_page(p, raw) == 7


def test_perceive_w8_scan_targets_cert_page(tmp_path, monkeypatch):
    p = tmp_path / "w8ben_form.pdf"
    d = fitz.open()
    for _ in range(8):
        d.new_page()
    d.save(p)
    d.close()
    survey = [(1 if i == 1 else 0, i, t, 0) for i, t in enumerate(W8_PAGES)]
    monkeypatch.setattr(stage_a, "_survey_scanned_pdf", lambda *a: survey)
    monkeypatch.setattr(stage_a, "_deep_read_pages",
                        lambda path, picks, rd, raw, uv: None)
    raw = stage_a.perceive(p, "w9", tmp_path, use_vision=False)
    assert raw.w8_cert_page == 7
    assert set(raw.pages_used) >= {0, 7}   # Part I + certification always read


def test_bank_scan_signature_prefers_non_w9_page(tmp_path):
    """A bank packet whose only 'Sign Here' hints sit on the W-9 page must
    not probe the W-9's signature for the bank document (P3/D root fix)."""
    p = tmp_path / "packet.pdf"
    d = fitz.open()
    d.new_page()
    d.new_page()
    d.save(p)
    d.close()
    raw = RawDoc(path=str(p), sha256="a" * 16, ext=".pdf", doc_class="bank",
                 pages_used=[0, 1], w9_pages=[1],
                 survey_texts={0: "banking information sheet",
                               1: "Form W-9 ... Sign Here ... certification"})
    assert _signature_page(p, raw) == 0    # non-W9 page wins the fallback
