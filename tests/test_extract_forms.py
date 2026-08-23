"""The schema readers over the offline extractor: W-9 by layout, bank documents
by label — no model, every value carrying the consensus status."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mdmdoc.extract import api
from mdmdoc.extract.forms import bank as bank_reader, w9 as w9_reader
from mdmdoc.extract.forms.common import vote


def _ln(text, x0, y0, x1, y1):
    return {"text": text, "bbox_pct": [x0, y0, x1, y1]}


def _w9_page(name="ACME WIDGETS LLC", biz="", street="12 MAIN ST", csz="SPRINGFIELD, IL 62704",
             ein="123456789", tick_at="llc", llc_letter="S"):
    """A Rev. 3-2024 page as two engines read it: the text layer (exact boxes)
    and RapidOCR (slightly different boxes, same words)."""
    labels = [
        ("1 Name of entity/individual. An entry is required. (For a sole proprietor", 10, 12.3, 94, 13.3),
        ("entity's name on line 2.)", 12, 13.3, 24, 14.4),
        ("2 Business name/disregarded entity name, if different from above.", 10, 16.8, 45, 17.9),
        ("3a Check the appropriate box for federal tax classification of the entity", 10, 19.9, 73, 20.9),
        ("only one of the following seven boxes.", 12, 20.9, 32, 22.0),
        ("Individual/sole proprietor", 14.2, 22.8, 26.9, 23.8), ("C corporation", 31.6, 22.8, 38.7, 23.8),
        ("S corporation", 43.4, 22.8, 50.4, 23.8), ("Partnership", 55.2, 22.8, 61.0, 23.8),
        ("Trust/estate", 65.8, 22.8, 71.9, 23.8),
        ("LLC. Enter the tax classification (C = C corporation, S = S corporation, P = Partnership)", 14.1, 24.5, 58.9, 25.5),
        ("Other (see instructions)", 14.1, 29.1, 26.3, 30.1),
        ("5 Address (number, street, and apt. or suite no.). See instructions.", 10, 35.0, 44.6, 36.0),
        ("Requester's name and address (optional)", 64.2, 35.0, 85.2, 36.0),
        ("6 City, state, and ZIP code", 10, 38.0, 24.6, 39.1),
        ("7 List account number(s) here (optional)", 10, 41.1, 31.2, 42.1),
        ("Social security number", 68.9, 45.5, 81.4, 46.5),
        ("Employer identification number", 68.9, 51.5, 86.3, 52.6),
        ("Part II Certification", 6.5, 56.0, 23.7, 57.5),
    ]
    values = [(name, 9.9, 15.3, 52, 16.4)]
    if biz:
        values.append((biz, 9.9, 18.4, 30, 19.5))
    values += [(street, 9.9, 36.5, 26.8, 37.7), (csz, 9.9, 39.6, 22.2, 40.7)]
    ein_lines = [(d, 69.0 + i * 2.4, 54.1, 69.8 + i * 2.4, 55.2) for i, d in enumerate(ein)]
    tl = [_ln(*l) for l in labels + values + ein_lines]
    box_x = {"llc": 12.2, "corporation_c": 29.7, "individual_sole_prop": 12.3}[tick_at]
    box_y = 24.6 if tick_at == "llc" else 22.9
    tl.append(_ln("✔", box_x, box_y, box_x + 0.7, box_y + 0.7))
    if tick_at == "llc" and llc_letter:
        tl.append(_ln(llc_letter, 70.1, 24.5, 71.1, 25.6))
    ro = [_ln(l[0], l[1] - 0.3, l[2] - 0.2, l[3] + 0.2, l[4] + 0.2) for l in labels + values + ein_lines]
    if tick_at == "llc" and llc_letter:
        ro.append(_ln(llc_letter, 69.7, 24.2, 71.4, 25.6))
    readings = {"textlayer": "\n".join(l["text"] for l in tl) + "\nForm W-9 (Rev. 3-2024)",
                "rapidocr:auto": "\n".join(l["text"] for l in ro) + "\nForm W-9 (Rev. 3-2024)"}
    return {"page": 0, "size": [1545, 2000], "lines": {"textlayer": tl, "rapidocr:auto": ro},
            "readings": readings, "fields": [], "transcript": readings["textlayer"]}


def test_w9_reads_every_field_by_layout():
    doc = {"pages_out": [_w9_page(biz="ACME DBA")]}
    fields, extra = w9_reader.read(doc)
    assert fields["line1_name"]["value"] == "ACME WIDGETS LLC"
    assert fields["line1_name"]["status"] == "confirmed"          # two engine families agree
    assert fields["line2_business_name"]["value"] == "ACME DBA"
    assert fields["address_street"]["value"] == "12 MAIN ST"
    assert (fields["address_city"]["value"], fields["address_state"]["value"], fields["address_zip"]["value"]) == \
        ("SPRINGFIELD", "IL", "62704")
    assert fields["tin"]["value"] == "123456789" and fields["tin_type"]["value"] == "ein"
    assert fields["tin"]["pretty"] == "12-3456789"
    # the digits came one per line: the TIN's box is the union of those lines
    assert fields["tin"]["bbox_pct"] and fields["tin"]["bbox_pct"][0] < 70 and fields["tin"]["bbox_pct"][2] > 88
    assert fields["classification"]["value"] == "llc"
    assert fields["llc_tax_class"]["value"] == "S"
    assert extra["w9_revision"] == "3-2024"
    # every value points at the page
    assert fields["line1_name"]["bbox_pct"] and fields["line1_name"]["page"] == 0


def test_w9_printed_text_never_becomes_a_value():
    doc = {"pages_out": [_w9_page(biz="")]}
    fields, _ = w9_reader.read(doc)
    assert fields["line2_business_name"]["status"] == "absent"
    assert "entity" not in fields["line1_name"]["value"].lower()


def test_w9_c_corporation_is_not_s_corporation():
    page = _w9_page(tick_at="corporation_c", llc_letter="")
    fields, _ = w9_reader.read({"pages_out": [page]})
    assert fields["classification"]["value"] == "corporation_c"
    a = w9_reader.find_anchors(page)
    assert a["box:corporation_c"]["x0"] < a["box:corporation_s"]["x0"]


def test_w9_box_is_found_on_the_render_and_its_fill_measured(tmp_path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1545, 2000), "white")
    dr = ImageDraw.Draw(img)
    # seven squares on the classification row; the third one ticked
    for i in range(7):
        x = 200 + i * 160
        dr.rectangle([x, 450, x + 24, 474], outline="black", width=2)
        if i == 2:
            dr.line([x + 4, 462, x + 11, 470], fill="black", width=3)
            dr.line([x + 11, 470, x + 21, 453], fill="black", width=3)
    # the label is right of each square; the window is derived from it
    fills = []
    for i in range(7):
        x = 200 + i * 160
        anchor = {"x0": (x + 32) / 1545 * 100, "y0": 450 / 2000 * 100, "x1": (x + 120) / 1545 * 100,
                  "y1": 474 / 2000 * 100}
        fill, rect = w9_reader._box_fill(img, w9_reader._search_window(anchor), 1.2)
        assert rect is not None, "the square's four edges are there to be found"
        fills.append(fill)
    assert fills[2] > 0.15 and all(f < 0.03 for j, f in enumerate(fills) if j != 2)


def test_vote_needs_two_families_to_confirm():
    assert vote({"textlayer": "Acme Inc", "rapidocr:auto": "ACME INC."})[1] == "confirmed"
    assert vote({"tess:eng": "Acme Inc", "tess:kor": "Acme Inc"})[1] == "review"        # same family twice
    raw, status, voices = vote({"textlayer": "Acme Inc", "rapidocr:auto": "Acne lnc"})
    assert status == "review" and raw == "Acme Inc"                                      # the most faithful engine


def test_bank_reader_labels_and_tokens():
    page = {"page": 0, "size": [1545, 2000], "lines": {
        "textlayer": [_ln("Account holder: ACME GmbH", 10, 30, 40, 31), _ln("Bank: Sparkasse Musterstadt", 10, 32, 40, 33),
                      _ln("IBAN DE89 3704 0044 0532 0130 00", 10, 34, 50, 35)],
        "rapidocr:auto": [_ln("Account holder: ACME GmbH", 10, 30, 40, 31), _ln("Bank: Sparkasse Musterstadt", 10, 32, 40, 33)]},
        "readings": {"textlayer": "Account holder: ACME GmbH\nBank: Sparkasse Musterstadt\nIBAN DE89 3704 0044 0532 0130 00\nCurrency: EUR",
                     "rapidocr:auto": "Account holder: ACME GmbH\nBank: Sparkasse Musterstadt\nIBAN DE89 3704 0044 0532 0130 00"},
        "fields": [{"value": "IBAN:DE89370400440532013000", "pretty": "DE89 3704 0044 0532 0130 00", "label": "IBAN",
                    "kind": "IBAN", "group": "bank", "status": "checksum_ok", "voices": ["textlayer"], "families": ["textlayer"],
                    "line": 2, "bbox_pct": [10, 34, 50, 35], "crop": None}]}
    fields, extra = bank_reader.read({"pages_out": [page]})
    assert fields["iban"]["value"] == "DE89370400440532013000" and fields["iban"]["status"] == "checksum_ok"
    assert fields["account_holder"]["value"] == "ACME GmbH" and fields["account_holder"]["status"] == "confirmed"
    assert fields["bank_name"]["value"] == "Sparkasse Musterstadt"
    assert fields["bank_country"]["value"] == "DE"          # from the IBAN, no country line
    assert fields["currency"]["value"] == "EUR" and fields["currency"]["status"] == "review"
    assert fields["account_number"]["status"] == "absent"


def test_doc_class_from_type_or_identifiers():
    assert api.doc_class_of("W-9") == "w9"
    assert api.doc_class_of("bank confirmation letter") == "bank"
    assert api.doc_class_of("unknown", {"iban": {"value": "DE89..."}}) == "bank"
    assert api.doc_class_of("invoice", {"iban": {"value": ""}}) == "other"


def test_capabilities_reports_engines_without_raising():
    caps = api.capabilities()
    assert caps["api_version"] == api.API_VERSION
    assert isinstance(caps["engines"], list) and isinstance(caps["reasons"], dict)


def test_api_reads_a_synthetic_w9_with_the_text_layer_only(tmp_path):
    import fitz
    pdf = tmp_path / "w9.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    rows = [
        (75, 37, "Form W-9 (Rev. March 2024)  Request for Taxpayer Identification Number and Certification"),
        (75, 100, "1 Name of entity/individual. An entry is required. (For a sole proprietor or disregarded entity"),
        (62, 125, "ACME WIDGETS LLC"),
        (62, 140, "2 Business name/disregarded entity name, if different from above."),
        (62, 162, "3a Check the appropriate box for federal tax classification of the entity/individual."),
        (88, 187, "Individual/sole proprietor"), (195, 187, "C corporation"), (268, 187, "S corporation"),
        (340, 187, "Partnership"), (405, 187, "Trust/estate"),
        (88, 201, "LLC. Enter the tax classification (C = C corporation, S = S corporation, P = Partnership)"),
        (88, 238, "Other (see instructions)"),
        (62, 285, "5 Address (number, street, and apt. or suite no.). See instructions."),
        (62, 300, "12 MAIN ST"),
        (62, 308, "6 City, state, and ZIP code"),
        (62, 323, "SPRINGFIELD, IL 62704"),
        (62, 333, "7 List account number(s) here (optional)"),
        (425, 368, "Social security number"),
        (425, 416, "Employer identification number"),
        (40, 450, "Part II Certification"),
    ]
    for x, y, t in rows:
        page.insert_text((x, y), t, fontsize=7)
    for i, d in enumerate("123456789"):
        page.insert_text((425 + i * 14, 440), d, fontsize=9)
    page.insert_text((75, 207), "✔", fontsize=8)
    doc.save(pdf)
    res = api.extract_for_consolidator(pdf, out_dir=tmp_path / "out", engines=["textlayer"])
    assert res["api_version"] == 1 and res["doc_class"] == "w9"
    f = res["fields"]
    assert f["line1_name"]["value"] == "ACME WIDGETS LLC"
    assert f["line1_name"]["status"] == "review"               # one engine only
    assert f["address_zip"]["value"] == "62704"
    assert f["tin"]["value"] == "123456789"
    assert (tmp_path / "out" / "extract.json").exists()
    assert api.render_page(pdf, tmp_path / "out" / "render", 0).exists()
    assert api.page_count(pdf) == 1


CORPUS = Path(__file__).resolve().parents[1] / "bench" / "corpus"


@pytest.mark.skipif(not os.environ.get("MDMDOC_CORPUS_TESTS") or not CORPUS.exists(),
                    reason="set MDMDOC_CORPUS_TESTS=1 — reads the real W-9s with tesseract + RapidOCR (minutes)")
def test_corpus_w9s_are_read(tmp_path):
    import re
    files = sorted(f for f in CORPUS.iterdir() if re.search(r"w-?9", f.name, re.I) and "W-8" not in f.name)
    assert files
    for f in files:
        res = api.extract_for_consolidator(f, out_dir=tmp_path / f.stem[:20], max_pages=1)
        assert res["doc_class"] == "w9", f.name
        fl = res["fields"]
        assert fl["line1_name"]["status"] != "absent", f.name
        assert fl["tin"]["status"] != "absent", f.name
        assert fl["classification"]["status"] == "confirmed", (f.name, fl["classification"]["evidence"])
