"""S2 (quality wave): the signature vision ensemble — e-sign short-circuit,
err votes distinct from negatives, bounded escalation on genuine disagreement,
tie -> unsigned (safe), contested -> HARD confidence signal."""
import fitz
import pytest

from mdmdoc import confidence, stage_a, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc, signature_probe


@pytest.fixture()
def letter_pdf(tmp_path):
    p = tmp_path / "letter.pdf"
    d = fitz.open()
    pg = d.new_page()
    pg.insert_text((72, 700), "Sincerely,")
    pg.insert_text((72, 730), "Jordan Q. Sample")
    d.save(p)
    d.close()
    return p


class FakeVision:
    """Scripted vote queue; each call pops the next reply."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, prompt, images, **kw):
        self.calls += 1
        if not self.replies:
            raise AssertionError("unexpected extra vision call")
        r = self.replies.pop(0)
        return (r, True) if r is not None else (None, False)


POS_HAND = {"handwritten_signature": True, "stamp": False,
            "date_near_signature": "", "evidence": "ink strokes"}
POS_STAMP = {"handwritten_signature": False, "stamp": True,
             "date_near_signature": "", "evidence": "round stamp"}
NEG = {"handwritten_signature": False, "stamp": False,
       "date_near_signature": "", "evidence": "typed text only"}


def _raw(path, doc_class="bank", text="bank letter body"):
    r = RawDoc(path=str(path), sha256="a" * 16, ext=".pdf", doc_class=doc_class)
    r.raw_text = text
    r.pages_used = [0]
    return r


def _run(monkeypatch, letter_pdf, tmp_path, replies, raw=None, cap=None):
    fake = FakeVision(replies)
    monkeypatch.setattr(stage_a.mc, "generate_json_vision", fake)
    if cap is not None:
        monkeypatch.setenv("MDMDOC_SIG_VISION_CAP", str(cap))
    r = raw or _raw(letter_pdf)
    signature_probe(letter_pdf, r, tmp_path)
    return r, fake


def test_agree_positive_single_call(monkeypatch, letter_pdf, tmp_path):
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [POS_HAND])
    assert fake.calls == 1
    sp = r.signature_probe
    assert sp["handwritten_signature"] is True
    assert sp["contested"] is False and sp["uncertain"] is False
    assert sp["escalated"] is False


def test_agree_negative_two_calls(monkeypatch, letter_pdf, tmp_path):
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [NEG, NEG])
    assert fake.calls == 2                       # band + page fallback
    sp = r.signature_probe
    assert sp["handwritten_signature"] is False and sp["stamp"] is False
    assert sp["contested"] is False


def test_ea_band_page_disagree_escalates(monkeypatch, letter_pdf, tmp_path):
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [NEG, POS_HAND, POS_HAND])
    assert fake.calls == 3                       # band, page, band_hi (E-A)
    sp = r.signature_probe
    assert sp["escalated"] is True
    assert sp["handwritten_signature"] is True   # 2 pos vs 1 neg
    assert sp["contested"] is True and sp["uncertain"] is True


def test_fold_tie_is_unsigned_and_contested_is_hard(monkeypatch, letter_pdf, tmp_path):
    r, _ = _run(monkeypatch, letter_pdf, tmp_path, [NEG, POS_HAND, NEG])
    sp = r.signature_probe
    assert sp["handwritten_signature"] is False  # 1 pos vs 2 neg -> negative
    assert sp["contested"] is True
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False}
    stage_b._resolve_signature(ext, r)
    a = confidence.assess(ext)
    assert a["level"] == "low"
    assert any("contested" in x for x in a["reasons"])


def test_eb_stamp_only_hunts_handwriting(monkeypatch, letter_pdf, tmp_path):
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [POS_STAMP, NEG])
    assert fake.calls == 2                       # band(stamp) + crop_b3 (E-B)
    assert r.signature_probe["votes"]["crop_b3"] == "neg"
    assert r.signature_probe["escalated"] is True


def test_ec_typed_system_vs_positive(monkeypatch, letter_pdf, tmp_path):
    raw = _raw(letter_pdf, text="This is a computer generated confirmation.")
    r, fake = _run(monkeypatch, letter_pdf, tmp_path,
                   [POS_HAND, POS_HAND], raw=raw)
    assert fake.calls == 2                       # band + band_hi (E-C)
    sp = r.signature_probe
    assert sp["uncertain"] is True               # typed-system vs positive fold


def test_cap_limits_probe_count(monkeypatch, letter_pdf, tmp_path):
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [NEG], cap=1)
    assert fake.calls == 1                       # page fallback blocked by cap
    sp = r.signature_probe
    assert sp["probes_used"] == 1 and sp["cap"] == 1
    assert sp["handwritten_signature"] is False


def test_esign_short_circuit_zero_calls(monkeypatch, letter_pdf, tmp_path):
    raw = _raw(letter_pdf, text="DocuSign Envelope ID: X. Electronically signed.")
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [], raw=raw)
    assert fake.calls == 0
    assert r.signature_probe["esign_short_circuit"] is True
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False}
    stage_b._resolve_signature(ext, r)
    assert ext.fields["signed"] is True
    assert ext.fields["signature_kind"] == "electronic"


def test_err_votes_no_escalation_unverified(monkeypatch, letter_pdf, tmp_path):
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [None])
    assert fake.calls == 1                       # err -> no page probe, no escalation
    sp = r.signature_probe
    assert sp.get("no_visual_verdict") is True and sp["uncertain"] is True


def test_trail_persisted_and_scrubbed(monkeypatch, letter_pdf, tmp_path):
    r, _ = _run(monkeypatch, letter_pdf, tmp_path, [POS_HAND])
    assert r.signature_probe["trail"][0]["probe"] == "band"
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False}
    stage_b._resolve_signature(ext, r)
    assert ext.signature_probe["trail"][0]["vote"] == "pos-hand"
    assert ext.signature_probe["probes_used"] == 1


def test_page_stamp_decisive_for_bank(monkeypatch, letter_pdf, tmp_path):
    """A red company seal (JP 角印) sits off the signature line: band reads NEG,
    the full-PAGE read sees the stamp. For a bank doc that page-level stamp wins
    over the signature-band negatives (real case: Lilycolor 1 stamp vs 2 neg)."""
    r, fake = _run(monkeypatch, letter_pdf, tmp_path, [NEG, POS_STAMP, NEG])
    assert fake.calls == 3                        # band, page, band_hi (E-A)
    sp = r.signature_probe
    assert sp["votes"]["page"] == "pos-stamp"
    assert sp["stamp"] is True                    # elevated despite 1 pos vs 2 neg
    assert sp["contested"] is True                # still flagged for the eye
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False}
    stage_b._resolve_signature(ext, r)
    assert ext.fields["signed"] is True
    assert ext.fields["signature_kind"] == "stamp"


def test_page_stamp_not_decisive_for_w9(monkeypatch, letter_pdf, tmp_path):
    """The page-stamp elevation is bank-only — a stamp is not a W-9 signature."""
    raw = _raw(letter_pdf, doc_class="w9")
    r, _ = _run(monkeypatch, letter_pdf, tmp_path, [NEG, POS_STAMP, NEG], raw=raw)
    assert r.signature_probe["stamp"] is False    # 1 pos vs 2 neg, no elevation


def test_seal_label_without_visible_seal_stays_unsigned(monkeypatch, letter_pdf, tmp_path):
    """Egor's decision: presence of the 印（角印でOK）label is NOT evidence — only a
    vision-detected seal signs. Text says 印 but the probe sees nothing -> unsigned."""
    raw = _raw(letter_pdf, text="口座名義 リリカラ株式会社オフィス\n印（角印でOK）\n")
    r, _ = _run(monkeypatch, letter_pdf, tmp_path, [NEG, NEG], raw=raw)
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"signed": False}
    stage_b._resolve_signature(ext, r)
    assert ext.fields["signed"] is False
