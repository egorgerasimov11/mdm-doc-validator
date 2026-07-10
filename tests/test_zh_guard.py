"""G2: the Chinese-form guard — an 11-digit CN mobile in a phone context is
not an account number; labeled 账号/账户 fields rescue the real value."""
from mdmdoc import stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

ZH_NOTICE = ("会议通知\n开户银行: 中国工商银行北京分行\n"
             "收款账号: 6222 0202 0000 1234 567\n户名: 某某协会\n"
             "联系人: 王伟  电话: 13712346060\n")


def _run(acct, text=ZH_NOTICE):
    ext = Extraction(doc_class="bank", doc_type="payment_instructions")
    ext.fields = {"account_number": acct, "bank_country": ""}
    raw = RawDoc(path="x.pdf", sha256="f" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = text
    stage_b._fix_zh_form(ext, raw)
    return ext


def test_mobile_dropped_and_labeled_account_rescued():
    ext = _run("13712346060")
    assert ext.fields["account_number"] == "6222020200001234567"
    assert any("CN mobile" in w for w in ext.warnings)


def test_labeled_account_fills_empty():
    ext = _run("")
    assert ext.fields["account_number"] == "6222020200001234567"
    assert ext.provenance["account_number"]["source"] == "ocr-regex"


def test_country_inferred_cn():
    ext = _run("")
    assert ext.fields["bank_country"] == "CN"


def test_real_account_kept():
    ext = _run("6222020200001234567")
    assert ext.fields["account_number"] == "6222020200001234567"


def test_jp_document_not_touched():
    text = "口座番号 1234567\n銀行\n"
    ext = _run("13712346060", text=text)
    assert ext.fields["account_number"] == "13712346060"   # zh guard stays out
    assert ext.fields["bank_country"] == ""
