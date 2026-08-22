"""Loop detector: structural rules calibrated on the benchmark (0 false positives on
116 gold pages + 246 normal VLM pages, 33/33 looped pages caught)."""
from mdmdoc.extract import loops as L


def test_repeated_lines_are_a_loop():
    text = "Header\n" + "| 30003 | 02110 | 00037262223 | 29 |\n" * 8
    ok, why = L.looks_looped(text)
    assert ok and "repeated" in why


def test_inline_cell_loop_is_a_loop():
    unit = "| Identification Internationale (IBAN) "
    text = "| Banque | Guichet |\n" + unit * 260          # ~10 KB on one line
    ok, why = L.looks_looped(text)
    assert ok and "inline" in why


def test_honest_long_page_is_not_a_loop():
    lines = [f"Transaction {i:04d}  2026-0{i % 9 + 1}-12  EUR {i * 13.37:.2f}  ref INV-{i}" for i in range(60)]
    assert not L.looks_looped("\n".join(lines))[0]


def test_short_text_never_loops():
    assert not L.looks_looped("ATREEC\nATREEC\nATREEC")[0]
    assert not L.looks_looped("")[0]


def test_empty_table_rows_do_not_count():
    text = "| Name | Value |\n|---|---|\n" + "|  |  |\n" * 20 + "| EIN | 84-0273800 |"
    assert not L.looks_looped(text)[0]


def test_dominant_line_rule():
    filler = [f"line {i} with some honest content" for i in range(12)]
    dominant = ["SOCIETE GENERALE RELEVE"] * 8
    mixed = [x for pair in zip(filler, dominant) for x in pair] + filler[:4]   # never 6 in a row
    ok, why = L.looks_looped("\n".join(mixed))
    assert ok and "of the page" in why


def test_block_repeat_rule_and_real_rib_margin():
    block = ["SOCIETE GENERALE", "RELEVES D'IDENTITE BANCAIRE", "TITULAIRE DU COMPTE", "ATREEC",
             "| 30003 | 02110 | 00037262223 | 29 |", "IBAN FR76 3000 3021 1000 0372 6222 329"]
    six = "\n".join(block * 6)          # a genuine RIB sheet: six detachable slips
    assert not L.looks_looped(six)[0]
    twelve = "\n".join(block * 12)      # the model re-emitting the slip until the token limit
    ok, why = L.looks_looped(twelve)
    assert ok and "block" in why


def test_collapse_keeps_one_copy_in_order():
    text = "A first\n" + "B repeated\n" * 9 + "C last"
    assert L.collapse_repeats(text) == "A first\nB repeated\nC last"
    unit = "| IBAN FR76 3000 3021 |"
    long = "head\n" + unit * 40 + "\ntail"
    c = L.collapse_repeats(long)
    assert c.count("IBAN") == 1 and c.startswith("head") and c.endswith("tail")
    block = ["SG", "RELEVES D'IDENTITE BANCAIRE", "| 30003 | 02110 |"]
    collapsed = L.collapse_repeats("\n".join(block * 12))
    assert collapsed.split("\n") == block


def test_collapse_is_identity_on_normal_text():
    text = "Form W-9\nName: Qwest Corporation\nEIN 84-0273800\n\nSign here"
    assert L.collapse_repeats(text) == text
