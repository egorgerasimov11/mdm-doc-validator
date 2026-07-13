"""JP domestic bank-form guard (_fix_jp_form): OCR now recovers the Japanese
labels, so the deterministic backstop maps/repairs 銀行名 / 支店名 / 口座名義 /
普通・当座, and clears the two fields the extractor model reliably mis-maps on
these forms — the branch NAME dropped into the numeric branch_code, and the
company 住所 lifted into bank_address (real case: Lilycolor)."""
from mdmdoc import stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

# the labels tesseract recovers (spaces between CJK glyphs are collapsed inside
# the guard); values match the real Lilycolor letter.
JP_LETTER = (
    "2026 年 7 月 6 日\n"
    "リヴァノヴァ株式会社\n岩佐宛\n"
    "リリカラ株式会社\n印（角印でOK）\n"
    "住所 東京都港区西新橋1-2-9\n"
    "電話番号 03-6895-5350\n"
    "下記の通り、口座情報をお知らせします。\n記\n"
    "銀行名 : 三井住友銀行\n"
    "支店名 : 新宿西口支店\n"
    "口座番号 : 普通 6790894\n"
    "口座名義 : リリカラ株式会社オフィス\n以上\n"
)


def _run(fields, text=JP_LETTER):
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    base = {k: "" for k in ("account_holder", "account_type", "bank_name",
                            "bank_country", "bank_address", "branch_code",
                            "branch_name", "account_number")}
    base.update(fields)
    ext.fields = base
    raw = RawDoc(path="x.pdf", sha256="f" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = text
    stage_b._fix_jp_form(ext, raw)
    return ext


def test_labels_rescued_when_model_left_them_empty():
    ext = _run({"account_number": "6790894"})
    f = ext.fields
    assert f["bank_name"] == "三井住友銀行"
    assert f["account_holder"] == "リリカラ株式会社オフィス"
    assert f["branch_name"] == "新宿西口支店"
    assert f["account_type"] == "普通口座 (ordinary account)"
    assert f["bank_country"] == "JP"


def test_backstop_never_overwrites_a_model_value():
    ext = _run({"account_number": "6790894",
                "bank_name": "SMBC", "account_holder": "Lilycolor Co."})
    assert ext.fields["bank_name"] == "SMBC"           # model value kept
    assert ext.fields["account_holder"] == "Lilycolor Co."


def test_branch_name_in_branch_code_is_relocated():
    # the model dropped the branch NAME into the numeric branch_code
    ext = _run({"account_number": "6790894", "branch_code": "新宿西口支店"})
    assert ext.fields["branch_code"] == ""             # cleared: not numeric
    assert ext.fields["branch_name"] == "新宿西口支店"
    assert any("branch_code cleared" in c for c in ext.crosscheck)


def test_numeric_branch_code_is_kept():
    ext = _run({"account_number": "6790894", "branch_code": "258"})
    assert ext.fields["branch_code"] == "258"


def test_company_address_cleared_from_bank_address():
    ext = _run({"account_number": "6790894",
                "bank_address": "東京都港区西新橋1-2-9"})
    assert ext.fields["bank_address"] == ""
    assert any("bank_address cleared" in c for c in ext.crosscheck)


def test_current_account_type():
    text = JP_LETTER.replace("普通 6790894", "当座 6790894")
    ext = _run({"account_number": "6790894"}, text=text)
    assert ext.fields["account_type"] == "当座預金 (current account)"


def test_fullwidth_and_spaced_labels_normalise():
    # tesseract inserts spaces between CJK glyphs and JP forms print full-width
    # digits — both must resolve through NFKC + collapse inside the guard
    text = ("銀行 名 ： 三井 住友 銀行\n"
            "支店 名 ： 新宿 西口 支店\n"
            "口座 番号 ： 普通 ６７９０８９４\n"
            "口座 名 義 ： リリ カラ 株式 会社 オフィス\n")
    ext = _run({"account_number": "6790894"}, text=text)
    assert ext.fields["bank_name"] == "三井住友銀行"
    assert ext.fields["account_holder"] == "リリカラ株式会社オフィス"
    assert ext.fields["branch_name"] == "新宿西口支店"


def test_non_jp_doc_not_touched():
    ext = _run({"account_number": "12345678", "bank_name": "Wells Fargo"},
               text="Wells Fargo Bank, N.A. Account 12345678 routing 121000248\n")
    assert ext.fields["bank_name"] == "Wells Fargo"
    assert ext.fields["bank_country"] == ""            # no JP markers -> guard returns early
