"""CJK OCR language selection: a Japanese page OCR'd in `eng` returns latin junk
that scores >= 8 realwords, so the old `realword_count < 8` gate never fired the
jpn retry and the whole document was lost. The gate is now an English-confidence
check + a CJK-character selector; orientation scoring counts CJK too, so an
upright Japanese page is not mistaken for noise and rotated into garbage."""
from mdmdoc import ocr
from mdmdoc.stage_a import _orientation_score

# what tesseract -l eng emits for the real Lilycolor page (case-jumbled latin)
JP_AS_ENG_JUNK = "FatOii 0, OMRe BASAL ET STA SSHEASRT SOA BITAPESIG VIAPHAAAT"
# what tesseract -l eng+jpn emits (spaces between glyphs)
JP_CJK = "銀行 名 : 三井 住友 銀行  口座 名 義 : リリカラ 株式 会社 オフィス"
REAL_EN = "Dear Sir, please confirm the bank account name and number for this company."


def test_cjk_char_count():
    assert ocr.cjk_char_count("三井住友銀行 6790894") == 6
    assert ocr.cjk_char_count("Wells Fargo 121000248") == 0


def test_english_confident_separates_junk_from_prose():
    assert ocr.english_confident(REAL_EN) is True          # fast path: skip CJK retry
    assert ocr.english_confident(JP_AS_ENG_JUNK) is False   # junk -> retry fires


def test_collapse_cjk_spaces_rejoins_glyphs_but_keeps_digit_gaps():
    out = ocr.collapse_cjk_spaces("銀行 名 : 三井 住友 銀行 普通 6790894")
    assert "三井住友銀行" in out
    assert "銀行名" in out
    assert "6790894" in out                                 # digit token preserved


def test_collapse_is_noop_for_latin():
    s = "Wells Fargo Bank N.A."
    assert ocr.collapse_cjk_spaces(s) == s


def test_orientation_score_counts_cjk():
    # an upright CJK page scores ~0 on latin realwords alone; the combined score
    # must rate it as legible so _best_rotation does NOT flip it hunting latin
    assert ocr.realword_count(JP_CJK) < 5
    assert _orientation_score(JP_CJK) >= 5
    assert _orientation_score(JP_CJK) > _orientation_score(JP_AS_ENG_JUNK[:4])


def test_have_cjk_true_when_jpn_installed():
    # the project host ships jpn (doctor lists it); guard the assertion on that
    if "jpn" in ocr._LANGS:
        assert ocr.have_cjk() is True
