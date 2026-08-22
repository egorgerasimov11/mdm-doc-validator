"""The offline guarantee: a value is handed over without a human only with two
independent families, three for confusable values, or its own checksum."""
from mdmdoc.extract import consensus as C


def _st(readings):
    return {v.value: v.status for v in C.consensus(readings)}


def test_two_families_confirm_same_family_does_not():
    s = _st({"tess:auto": "Account 4830 2291 0077", "ollama:qwen2.5vl:7b": "Account 4830 2291 0077"})
    assert s["483022910077"] == "confirmed"
    s = _st({"ollama:qwen2.5vl:7b@v170": "Account 4830 2291 0077", "ollama:gemma3:4b": "Account 4830 2291 0077"})
    assert s["483022910077"] == "review"                      # two VLMs are ONE family


def test_disagreement_goes_to_review():
    s = _st({"tess:auto": "Account 4830 2291 0077", "ollama:qwen2.5vl:7b": "Account 4830 2291 0078"})
    assert s["483022910077"] == "review" and s["483022910078"] == "review"


def test_checksum_rescues_a_single_reading():
    s = _st({"tess:auto": "IBAN DE75 6006 0000 0107 9000 00  Routing 026009593"})
    assert s["IBAN:DE75600600000107900000"] == "checksum_ok" and s["026009593"] == "checksum_ok"
    s = _st({"tess:auto": "IBAN DE75 6006 0000 0107 9000 01  Routing 026009594"})
    assert s["IBAN:DE75600600000107900001"] == "review" and s["026009594"] == "review"


def test_iban_cut_to_country_length_when_regex_runs_past():
    s = _st({"tess:auto": "IBAN DE75 6006 0000 0107 9000 00 EIN 84-0273800 Form W-9"})
    assert s["IBAN:DE75600600000107900000"] == "checksum_ok"
    assert "75600600000107900000" not in s                   # no digit shadow of the IBAN


def test_ein_shape_needs_us_context():
    hu = "Cégjegyzékszám: 01-1041043 K&H Bank Zrt."
    assert _st({"ollama:qwen2.5vl:7b": hu})["EIN:01-1041043"] == "review"
    us = "Form W-9 Employer identification number 84-0273800"
    assert _st({"ollama:qwen2.5vl:7b": us})["EIN:84-0273800"] == "checksum_ok"


def test_confusable_and_long_values_need_three_families():
    two = {"tess:auto": "SWIFT ICRAITRRISO", "ollama:qwen2.5vl:7b": "SWIFT ICRAITRRISO"}
    assert _st(two)["SWIFT:ICRAITRRISO"] == "review"
    three = dict(two, **{"rapidocr:auto": "BIC ICRAITRRISO"})
    assert _st(three)["SWIFT:ICRAITRRISO"] == "confirmed"
    long_two = {"tess:auto": "账号 1901026309100015523", "ollama:qwen2.5vl:7b": "账号 1901026309100015523"}
    assert _st(long_two)["1901026309100015523"] == "review"


def test_hidden_text_layer_value_is_never_auto():
    s = _st({"textlayer": "IBAN: NL09ABNA0103974504", "tess:auto": "Afschrift Zakelijke rekening 856,00"})
    assert s["IBAN:NL09ABNA0103974504"] == "review"
    alone = _st({"textlayer": "IBAN: NL09ABNA0103974504"})     # no visual voice at all
    assert alone["IBAN:NL09ABNA0103974504"] == "checksum_ok"


def test_score_consensus_counts_silent_errors():
    gold = "Account 4830 2291 0077\nRouting 026009593"
    sc = C.score_consensus(gold, {"tess:auto": "Account 4830 2291 0077 Routing 026009593 Ref 111122223333",
                                  "ollama:qwen2.5vl:7b": "Account 4830 2291 0077 Ref 111122223333"})
    assert sc["gold_values"] == 2 and sc["auto_found"] == 2 and sc["auto_share"] == 1.0
    assert sc["silent_errors"] == 1 and sc["silent_error_values"] == ["111122223333"]
    sc2 = C.score_consensus("Routing 026009593", {"tess:auto": "Routing 026 009 593"})
    assert sc2["silent_errors"] == 0 and sc2["auto_found"] == 1     # separators differ → same value
