"""Structured extraction: one primary transcript, labelled values, composite noise dropped."""
from mdmdoc.extract import extractor as X
from mdmdoc.extract import consensus as C
from mdmdoc.extract import engines as E


def test_primary_prefers_text_layer_then_vlm_then_rapidocr():
    r = {"tess:auto": "t", "rapidocr:auto": "r", "ollama:qwen2.5vl:7b@v200": "v", "textlayer": "l"}
    assert X.primary_reading(r) == ("textlayer", "l")
    r.pop("textlayer")
    assert X.primary_reading(r)[0].startswith("ollama:")
    r.pop("ollama:qwen2.5vl:7b@v200")
    assert X.primary_reading(r) == ("rapidocr:auto", "r")
    assert X.primary_reading({"textlayer": "   ", "tess:auto": "t"}) == ("tess:auto", "t")


RIB = ("TITULAIRE DU COMPTE\nATREEC\nDOMICILIATION : TOULOUSE METZ (02110)\n"
       "Banque  Guichet  N° de compte  Clé RIB\n30003  02110  00037262223  29\n"
       "Identification Internationale (IBAN)\nIBAN FR76 3000 3021 1000 0372 6222 329\nN° ADEME : FR231725_01YSGB")


def test_labels_from_table_header_same_line_and_previous_line():
    assert X._locate("02110", RIB)[:2] == ("Guichet", "02110")          # header beats the address line
    assert X._locate("30003", RIB)[:2] == ("Banque", "30003")
    assert X._locate("00037262223", RIB)[:2] == ("N° de compte", "00037262223")
    label, pretty, _ = X._locate("IBAN:FR7630003021100003726222329", RIB)
    assert pretty == "FR76 3000 3021 1000 0372 6222 329" and label in ("IBAN", "Identification Internationale (IBAN)")
    assert X._locate("231725", RIB)[0] == "N° ADEME"


def test_classify_by_token_and_label():
    assert X.classify("IBAN:DE75600600000107900000", "") == ("IBAN", "bank")
    assert X.classify("SWIFT:GENODES1BIA", "BIC") == ("BIC / SWIFT", "bank")
    assert X.classify("026009593", "ABA routing") == ("routing (ABA)", "bank")
    assert X.classify("62062215", "Bankleitzahl") == ("bank code", "bank")
    assert X.classify("070622640", "Telefon") == ("phone", "contact")
    assert X.classify("145787822", "USt-IdNr. DE") == ("tax id", "tax")
    assert X.classify("71717", "") == ("number", "other")


def test_composite_tesseract_tokens_are_dropped():
    readings = {"rapidocr:auto": RIB, "tess:auto": RIB.replace("30003  02110  00037262223  29", "30003 02110 00037262223 29")}
    verdicts = C.consensus(readings)
    fields = X.build_fields(verdicts, RIB, {}, (1000, 1400))
    values = {f["value"] for f in fields}
    assert "3000302110" not in values and "300030211000037262223" not in values
    assert {"30003", "02110", "00037262223"} <= values
    assert all(f["status"] == "confirmed" for f in fields if f["value"] in ("02110", "00037262223"))


def test_bbox_in_percent_from_ocr_lines():
    lines = {"rapidocr:auto": [{"text": "N° de compte 00037262223", "bbox": [100, 200, 500, 240]}]}
    verdicts = C.consensus({"rapidocr:auto": RIB, "tess:auto": RIB})
    f = next(f for f in X.build_fields(verdicts, RIB, lines, (1000, 2000)) if f["value"] == "00037262223")
    assert f["bbox_pct"] == [10.0, 10.0, 50.0, 12.0]


def test_scripts_of_text():
    assert E.scripts_of_text("계좌번호 302-0653-1998-81 SWIFT CODE NACFKRSE 예금종류 저축예금") == ["Hangul", "Latin"]
    assert E.scripts_of_text("RELEVES D'IDENTITE BANCAIRE SOCIETE GENERALE") == ["Latin"]
    assert E.scripts_of_text("Certyfikat bankowy ナ numer rachunku bankowego PL 12 3456") == ["Latin"]   # stray kana


def test_markdown_groups_and_primary_transcript():
    verdicts = C.consensus({"rapidocr:auto": RIB, "tess:auto": RIB})
    fields = X.build_fields(verdicts, RIB, {}, (1000, 1400))
    doc = {"file": "x/RIB.pdf", "doc_type": "RIB", "pages": 1, "engines": ["tess:auto", "rapidocr:auto"],
           "elapsed_s": 1.0, "transcript": RIB,
           "pages_out": [{"page": 0, "fields": fields, "primary_engine": "rapidocr:auto", "transcript": RIB}]}
    md = X.to_markdown(doc)
    assert "## Bank details" in md and "| 1 | Guichet | `02110` | confirmed |" in md
    assert md.count("TITULAIRE DU COMPTE") == 1                     # one transcript, no merged duplicates


def test_bbox_prefers_the_pure_cell_over_a_line_with_the_same_digits():
    lines = {"rapidocr:auto": [
        {"text": "DOMICILIATION : TOULOUSE METZ (02110)", "bbox": [172, 446, 555, 472]},
        {"text": "02110", "bbox": [322, 506, 384, 531]},
        {"text": "IBAN FR76 3000 3021 1000 0372 6222 329", "bbox": [172, 571, 537, 595]}]}
    box, _ = X._bbox_for("02110", lines, "30003  02110  00037262223  29")
    assert box == [322, 506, 384, 531]


def test_page_boxes_and_transcript_line_matching():
    lines = {"rapidocr:auto": [{"text": "Banque  Guichet", "bbox": [100, 200, 400, 230]},
                               {"text": "30003", "bbox": [100, 240, 160, 270]},
                               {"text": "SOCIETE GENERALE", "bbox": [100, 50, 400, 80]}]}
    boxes = X.page_boxes(lines, (1000, 2000))
    assert boxes[0]["bbox_pct"] == [10.0, 10.0, 40.0, 11.5] and len(boxes) == 3
    rows = X.transcript_lines_with_boxes("SOCIETE GENERALE\n\nBanque Guichet\n30003\nunrelated text here", boxes)
    seg = lambda i: rows[i]["segments"][0]["bbox_pct"] if rows[i]["segments"] else None
    assert seg(0) == [10.0, 2.5, 40.0, 4.0]
    assert seg(2) == [10.0, 10.0, 40.0, 11.5] and seg(3) == [10.0, 12.0, 16.0, 13.5]
    assert seg(1) is None and seg(4) is None
    # twin slips joined on one line: each copy goes to its own box
    twin = [{"text": "IBAN FR76 3000", "bbox_pct": [10, 30, 40, 31]}, {"text": "IBAN FR76 3000", "bbox_pct": [55, 30, 85, 31]}]
    r = X.transcript_lines_with_boxes("IBAN FR76 3000  IBAN FR76 3000", twin)[0]["segments"]
    assert [x["bbox_pct"][0] for x in r] == [10, 55]
