from mdmdoc.bench import metrics as M


def test_normalize_nfkc_quotes_dashes_markup():
    assert M.normalize("１２３ ＡＢＣ") == "123 ABC"
    assert M.normalize("“quoted” — ‘x’") == '"quoted" - \'x\''
    assert M.normalize("Name: [hw]Todd Hall[/hw] [signature] [seal: red round]") == "Name: Todd Hall"
    assert M.normalize("| a | b |\n|---|---|\n| 1 | 2 |") == "a b\n1 2"
    assert M.normalize("三井 住友 銀行") == "三井住友銀行"
    assert M.normalize("a   b\t c\n\n\nd") == "a b c\nd"
    assert M.normalize("Account No. 302-0653-1998-81", "loose") == "accountno302065319988 1".replace(" ", "")


def test_loose_maps_arabic_indic_digits():
    assert M.normalize("الرقم ٣٠٠١٢٣", "loose") == "الرقم300123"


def test_cer_basic():
    assert M.cer("hello world", "hello world") == 0.0
    assert M.cer("abcdefghij", "abcdefghiX") == 0.1
    assert M.cer("", "") == 0.0
    assert M.cer("", "junk") == 1.0
    # formatting-only differences vanish at loose level
    assert M.cer("IBAN: DE89 3704 0044 0532 0130 00", "iban de89370400440532013000", "loose") == 0.0


def test_wer_and_cjk_suppression():
    assert M.wer("the quick brown fox jumps", "the quick brown fox jumps") == 0.0
    assert M.wer("the quick brown fox jumps", "the quick brown cat jumps") == 0.2
    assert M.wer("계좌번호 302-0653-1998-81 예금종류 저축예금", "anything") is None


def test_line_align_recall_precision():
    gold = ["Bank of America", "Account 4830 2291 0077", "SWIFT BOFAUS3N", "Sincerely, Jane"]
    cand = ["SWIFT BOFAUS3N", "Bank of America", "Account 4830 2291 0077", "extra line here"]
    la = M.line_align(gold, cand)
    assert la.recall == 0.75 and la.precision == 0.75
    assert la.unmatched_gold == ["Sincerely, Jane"]
    assert la.unmatched_cand == ["extra line here"]
    # small OCR slips still align
    la2 = M.line_align(["Account 4830 2291 0077"], ["Account 4830 2291 0O77"])
    assert la2.recall == 1.0
    assert M.line_align([], []).recall == 1.0
    assert M.line_align(["x"], []).recall == 0.0


def test_digit_tokens_canonical_and_multiset():
    c = M.digit_tokens("Tel 055-366-7201, acct 302-0653-1998-81, 2013 년 01 월 22 일, 1588-2100 and 1588 2100")
    assert c["0553667201"] == 1
    assert c["3020653199881"] == 1
    assert c["2013"] == 1
    assert c["15882100"] == 2            # two spellings, same canonical token → multiset
    assert "01" not in c and "22" not in c


def test_id_tokens():
    c = M.id_tokens("IBAN: DE89 3704 0044 0532 0130 00  BIC COBADEFFXXX  EIN 84-0273800  SSN 123-45-6789")
    assert c["IBAN:DE89370400440532013000"] == 1
    assert c["SWIFT:COBADEFFXXX"] == 1
    assert c["EIN:84-0273800"] == 1
    assert c["SSN:123-45-6789"] == 1


def test_entity_recall_missing_token():
    gold = "acct 302-0653-1998-81 tel 055-366-7201 SWIFT NACFKRSE date 2013-01-22"
    cand = "acct 302-0653-1998-81 tel 055-366-7201 SWIFT NACFKRSE"
    r = M.entity_recall(gold, cand)
    assert r.total == 4 and r.found == 3 and r.recall == 0.75
    assert r.missing == ["20130122"]
    # order/format insensitive
    r2 = M.entity_recall(gold, "2013.01.22 … 0553667201 … NACFKRSE … 302 0653 1998 81")
    assert r2.recall == 1.0


def test_field_value_recall_base_and_loose_and_handwritten():
    fields = [
        {"label": "계좌번호", "value": "302-0653-1998-81", "handwritten": False},
        {"label": "예금주", "value": "남상욱", "handwritten": False},
        {"label": "SWIFT", "value": "NACFKRSE", "handwritten": False},
        {"label": "Date", "value": "1-6-26", "handwritten": True},
        {"label": "box", "value": "☑", "handwritten": False},       # ignored
        {"label": "tiny", "value": "5", "handwritten": False},      # ignored (<2 chars)
    ]
    cand = "계좌번호 302 0653 1998 81\n남상욱 님\nSWIFT CODE: NACFKRSE\nDate [hw]1-6-26[/hw]"
    allr, hwr = M.field_value_recall(fields, cand)
    assert allr.total == 4 and allr.recall == 1.0
    assert hwr.total == 1 and hwr.recall == 1.0
    allr2, hwr2 = M.field_value_recall(fields, "계좌번호 302-0653-1998-18\nSWIFT NACFKRSE")
    assert allr2.found == 1 and allr2.recall == 0.25
    assert "남상욱" in " ".join(allr2.missing)
    assert hwr2.recall == 0.0


def test_score_page_and_aggregation():
    gold = "Bank of America\nAccount 4830 2291 0077\nSWIFT BOFAUS3N\nRouting 026009593"
    fields = [{"label": "Account", "value": "4830 2291 0077"}, {"label": "SWIFT", "value": "BOFAUS3N"}]
    perfect = M.score_page(gold, fields, gold)
    assert perfect["cer"] == 0.0 and perfect["line_recall"] == 1.0
    assert perfect["entity"]["recall"] == 1.0 and perfect["field"]["recall"] == 1.0
    worse = M.score_page(gold, fields, "Bank of America\nAccount 4830 2291 0O77\nSWIFT BOFAUS3N")
    assert 0 < worse["cer"] < 0.35        # one dropped line (17 of ~68 chars) + one slip
    assert worse["entity"]["recall"] < 1.0 and worse["field"]["recall"] == 0.5
    doc = M.aggregate_pages([perfect, worse])
    assert doc["pages"] == 2 and 0 < doc["cer"] < 0.2 and doc["field_recall"] == 0.75
    agg = M.aggregate_docs([dict(doc, doc_id="a"), dict(M.aggregate_pages([perfect]), doc_id="b")])
    assert agg["docs"] == 2
    assert agg["field_recall_worst"] == 0.75 and agg["field_recall_worst_doc"] == "a"
    assert agg["cer_worst_doc"] == "a"
    ok, fails = M.passes("print", agg)
    assert not ok and any("field_recall" in f for f in fails)
    ok2, fails2 = M.passes("print", M.aggregate_docs([dict(M.aggregate_pages([perfect]), doc_id="b")]))
    assert ok2 and not fails2


def test_field_value_order_insensitive_and_truncated_gold():
    # the address is on the page but a neighbouring column split it in reading order
    cand = ("a wholly owned subsidiary\nof LivaNova PLC\n100 Cyberonics Blvd   Disclaimer: By signing\n"
            "Houston, TX 77058\nSigned by: B13472197ADB4901 full id")
    gold = [{"label": "Company address", "value": "100 Cyberonics Blvd Houston, TX 77058"},
            {"label": "Signed by", "value": "[signature] B13472197ADB490..."},   # gold truncated it
            {"label": "Bank", "value": "Wells Fargo Bank"}]                      # genuinely absent
    r, _ = M.field_value_recall(gold, cand)
    assert r.total == 3 and r.found == 2
    assert r.missing == ["Bank: Wells Fargo Bank"]
    # the fallback must not rescue a value made only of short common tokens
    r2, _ = M.field_value_recall([{"label": "x", "value": "by on"}], cand)
    assert r2.total == 1 and r2.found == 0        # scored, but no distinctive token → no rescue
    r3, _ = M.field_value_recall([{"label": "x", "value": "Cyberonics Wells"}], cand)
    assert r3.found == 0                          # one token missing → still a miss


def test_checkbox_and_writer_parentheticals_are_not_page_text():
    gold = [{"label": "3a", "value": "☑ LLC (other boxes unmarked: Individual/sole proprietor, C corporation)"},
            {"label": "3b", "value": "☐ (unmarked)"},
            {"label": "14a", "value": "☑ Italy"}]
    r, _ = M.field_value_recall(gold, "Federal tax classification LLC\nresident of Italy")
    assert r.total == 2 and r.recall == 1.0


def test_doc_time_and_percentile():
    pg = lambda lat: dict(M.score_page("Name: Acme\nEIN 84-0273800", [{"label": "Name", "value": "Acme"}],
                                        "Name: Acme\nEIN 84-0273800"), latency_s=lat)
    # 2 measured pages of a 4-page document: 10 s + 20 s → mean 15 × 4 = 60 s, extrapolated
    a = M.aggregate_pages([pg(10), pg(20)], pages_total=4)
    assert a["doc_time_s"] == 60.0 and a["extrapolated"] and a["pages_measured"] == 2 and a["pages_total"] == 4
    b = M.aggregate_pages([pg(10), pg(20)])
    assert b["doc_time_s"] == 30.0 and not b["extrapolated"]
    assert M.percentile_nearest_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert M.percentile_nearest_rank([5, 1], 0.9) == 5 and M.percentile_nearest_rank([], 0.9) is None
    docs = [dict(a, doc_id=f"d{i}", doc_time_s=t) for i, t in enumerate([20, 30, 40, 50, 55, 58, 59, 61, 70, 200])]
    agg = M.aggregate_docs(docs)
    assert agg["doc_time_p90_s"] == 70 and agg["doc_time_median_s"] == 55
    assert agg["within_60s_share"] == 0.7 and agg["docs_over_60s"] == ["d9", "d8", "d7"]
    assert agg["doc_time_worst_doc"] == "d9" and agg["extrapolated_docs"] == 10
    ok, fails = M.passes("print", agg)
    assert not ok and any("doc_time_p90 70s > 60s" in f for f in fails)
    hw = dict(agg, doc_time_p90_s=80)
    assert not any("doc_time" in f for f in M.passes("handwriting", hw)[1])
    assert not any("doc_time" in f for f in M.passes("print", dict(agg, doc_time_p90_s=None))[1])
