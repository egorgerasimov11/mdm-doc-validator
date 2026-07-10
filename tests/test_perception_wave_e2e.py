"""End-to-end shape locks for the perception wave (part B): the three real
failure shapes — F1 Altrum (garbage text layer + the good page unread),
F2 GVS (W-8 required pages), F3 Zajecka (a W-9 page's signature must never
sign the bank entity). Deterministic only — no model, no vision."""
import fitz

from mdmdoc import ladder, stage_a, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc, perceive


def _pdf(tmp_path, pages, name="doc.pdf"):
    p = tmp_path / name
    d = fitz.open()
    for text in pages:
        pg = d.new_page()
        y = 80
        for line in text.splitlines():
            pg.insert_text((72, y), line, fontsize=10)
            y += 16
    d.save(p)
    d.close()
    return p


# --- F1: garbage text layer reroutes; the ladder reaches the good page -------
def test_f1_garbage_reroute_then_ladder_reads_good_page(tmp_path, monkeypatch):
    pages = ["\x00\x01\x02\x03" * 200 + "account bank wire routing " * 3] * 3
    pdf = _pdf(tmp_path, pages)
    monkeypatch.setattr(stage_a, "_pdf_page_texts", lambda path, cap: (pages, 3))
    survey = [(0, 0, "garbage one", 0), (0, 1, "garbage two", 0),
              (8, 2, "This letter is to confirm the account details below.\n"
                     "Account holder: Fake Corp GmbH\n"
                     "IBAN DE89 3704 0044 0532 0130 00", 0)]
    monkeypatch.setattr(stage_a, "_survey_scanned_pdf",
                        lambda path, rd, dc: survey)

    def fake_deep(path, picks, rd, raw, use_vision):
        for _, i, t, _r in picks:
            raw.page_texts[i] = t
        raw.tesseract_text = "\n".join(t for _, _, t, _ in picks)

    monkeypatch.setattr(stage_a, "_deep_read_pages", fake_deep)
    raw = perceive(pdf, "bank", tmp_path, use_vision=False)
    assert raw.text_layer_garbage is True          # rerouted, not trusted
    assert 2 in raw.pages_used                     # the good page won the survey
    assert "Fake Corp GmbH" in raw.raw_text


def test_f1_ladder_closes_gap_left_by_first_pass(tmp_path, monkeypatch):
    """First pass read only garbage pages; the ladder deep-reads the promising
    unread page and the re-extract fills the holder."""
    pdf = _pdf(tmp_path, ["x", "y", "z"])
    raw = RawDoc(path=str(pdf), sha256="a" * 16, ext=".pdf", doc_class="bank")
    raw.text_layer_garbage = True
    raw.pages_used = [0, 1]
    raw.page_texts = {0: "garbage", 1: "garbage"}
    raw.raw_text = "garbage"
    raw.survey_texts = {0: "garbage", 1: "garbage",
                        2: "Account holder: Fake Corp GmbH\n"
                           "IBAN DE89 3704 0044 0532 0130 00"}
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_holder": "", "bank_name": "Fake Bank"}
    ext.escalated_because = ["bank-no-holder"]

    monkeypatch.setattr(stage_a, "_quick_ocr",
                        lambda p: raw.survey_texts[2])
    monkeypatch.setattr(stage_a, "_best_rotation", lambda p, t: (0, t))

    def fake_extract(raw_, quality=False, policy="masked", engine="auto",
                     injected_llm=None):
        e2 = Extraction(doc_class="bank", doc_type="bank_letter")
        holder = ("Fake Corp GmbH"
                  if "Fake Corp GmbH" in raw_.raw_text else "")
        e2.fields = {"account_holder": holder}
        return e2

    monkeypatch.setattr(stage_b, "extract", fake_extract)
    import time
    out, meta = ladder.climb(pdf, raw, ext, tmp_path, t0=time.time(),
                             quality=False, policy="masked", engine="auto",
                             use_vision=False)
    assert meta["used"] is True and meta["pages"] == [3]
    assert out.fields["account_holder"] == "Fake Corp GmbH"


# --- F2: W-8 required pages — Part I up front, certification at the end ------
def test_f2_w8_perceive_targets_part1_and_certification(tmp_path):
    pages = (["Form W-8BEN-E Certificate of Status of Beneficial Owner\n"
              "Part I Identification of Beneficial Owner\n"
              "1 Name of organization: Nord Fake GmbH"]
             + [f"Instructions boilerplate page {i}" for i in range(2, 8)]
             + ["Part XXX Certification\nSign Here\nDate (MM-DD-YYYY): 02-03-2026"])
    pdf = _pdf(tmp_path, pages, name="w8ben_e.pdf")
    raw = perceive(pdf, "w9", tmp_path, use_vision=False)
    assert raw.type_hint == "w8"
    assert raw.w8_cert_page == 7                   # the LAST page carries Part XXX
    assert 0 in raw.pages_used and 7 in raw.pages_used
    ext = stage_b.extract(raw, engine="deterministic")
    assert ext.doc_type == "w8"                    # forced by the subtype gate
    assert "legal_name" in ext.fields              # W-8 schema, not W-9


# --- F3: a W-9 page's signature never signs the bank entity ------------------
def test_f3_w9_page_signature_does_not_sign_bank_entity():
    raw = RawDoc(path="packet.pdf", sha256="b" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = "Supplier banking sheet. Account 12345678."
    raw.w9_pages = [1]
    raw.pages_used = [0, 1]
    raw.signature_probe = {"page": 1, "handwritten_signature": True,
                           "stamp": False, "uncertain": False,
                           "evidence": "ink strokes on the W-9 line"}
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False, "signature_evidence": ""}
    stage_b._resolve_signature(ext, raw)
    assert ext.fields["signed"] is False           # W-9 ink stays on the W-9
    assert any("W-9" in w or "w9" in w.lower() for w in ext.warnings)


def test_f3_bank_page_signature_still_counts():
    raw = RawDoc(path="packet.pdf", sha256="b" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = "Bank letter."
    raw.w9_pages = [1]
    raw.pages_used = [0, 1]
    raw.signature_probe = {"page": 0, "handwritten_signature": True,
                           "stamp": False, "uncertain": False,
                           "evidence": "ink strokes"}
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False, "signature_evidence": ""}
    stage_b._resolve_signature(ext, raw)
    assert ext.fields["signed"] is True
    assert ext.fields["signature_kind"] == "wet"
