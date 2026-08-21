"""Is this text *language*, or the soup a broken text layer / bad OCR produces?

`plausibility(text)` -> 0..1.  It is the gate that decides whether a PDF text
layer can be trusted at all.  The motivating failure: a scanned Korean
bankbook whose embedded OCR layer reads

    zt 4fla  q=€+  d€qql€  7l'J 6t{] 'J  <<t 1.4€.ei>>  r*+F_*

— which passes ocr.text_layer_garbage (few control chars, enough short latin
runs) and so was trusted, while the real page is Hangul.  Calibration targets
(see tests/test_extract_plausibility.py): that string < 0.6; every synthetic
PDF text layer and every clean digital document >= 0.75.

The score is deliberately script-agnostic AND unicode-table-free: any character above
U+007F that is not in the explicit symbol set counts as a letter of some other script.
That keeps Python and the ABAP twin (7.50, no character-category tables) computing the
SAME function rather than two similar ones — see PARITY.md.
"""
from __future__ import annotations

import re

from .. import ocr

TRUST_LAYER = 0.7          # below this a text layer is treated as absent (garbage 0.47-0.56, real docs >= 0.80)

_EDGE_PUNCT = "()[]{}<>\"'«»„“”‘’.,;:!?…-–—_/\\|*+=~^`·•●○■□▪"
_VOWELS = set("aeiouyàáâãäåæèéêëìíîïòóôõöøùúûüýāăąēėęěīįıōőœūůűųỳ")
_NUM_TOKEN = re.compile(r"^[+\-]?\d(?:[\d.,\-/:′″']*\d)?%?$|^\(?\d{2,4}\)?[\d\-. ]{3,}$")
_MIXED_OK = re.compile(r"^(?:[A-Za-z]{1,3}\d{1,6}[A-Za-z]?|\d{1,6}(?:st|nd|rd|th|er|re|ª|º|[A-Za-z]))$")
_UPPER_CODE = re.compile(r"^[A-Z0-9][A-Z0-9./-]{2,}$")
# The ONE explicit symbol set. To be registered in tools/parity/constants.json under
# the id plausibility_symbols (with the CONST marker) once the ABAP twin carries it —
# see PARITY.md. A character above U+007F
# that is NOT in here counts as a letter of some other script (CJK, Cyrillic, Arabic,
# accented Latin…) — that is how this gate stays unicode-table-free and therefore
# portable to ABAP 7.50, which has no character-category tables. Verified on the whole
# corpus: the discriminating power lives entirely in the ASCII range (the Korean
# mojibake that motivated this gate carries exactly one non-ASCII character: €).
_SYMBOLS = "{}[]<>|^~`=@#$%*\\€£¥₩§©®™°±×÷•·●○■□▪▫◆★☆←→↑↓☑☐✓✔✗✘"
_WEIRD_INSIDE = re.compile("[" + re.escape("{}[]<>|^~`=@#$%*\\€£¥₩§©®™°") + "]")


def _has_other_script(text: str) -> bool:
    """A character of a non-Latin script (or an accented Latin one): above U+007F and
    not a symbol. Replaces the former CJK-only test — same verdict on the corpus
    (max |delta| 0.023, zero threshold flips) and expressible in ABAP without tables."""
    return any(ord(c) > 127 and c not in _SYMBOLS for c in text)
_SHORT_OK = {
    "a", "i", "o", "y", "an", "at", "as", "be", "by", "do", "go", "he", "if", "in", "is", "it",
    "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we", "ok", "am", "pm", "de",
    "la", "le", "el", "en", "et", "du", "da", "di", "il", "un", "se", "es", "al", "lo", "su",
    "te", "ce", "ne", "je", "tu", "il", "ou", "au", "im", "zu", "ab", "um", "ja", "er", "es",
    "wo", "da", "и", "в", "на", "по", "не", "от", "до", "за", "из", "к", "с", "у", "о", "же",
    "но", "то", "ли", "ни", "да", "со", "ко", "об", "во", "я", "ты", "он", "мы",
    "nr", "no", "id", "eu", "uk", "us", "ch", "kr", "jp", "cn", "ru", "tr", "bv", "ag", "sa",
    "nv", "co", "lt", "ab", "gm", "kg", "cm", "mm", "ml", "hr", "tz", "vs", "re", "fw", "cc",
    "cf", "pp", "ph", "rm", "rs", "tx", "tv", "ok",
}


def _strip_edges(tok: str) -> str:
    return tok.strip(_EDGE_PUNCT)


_ASCII_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _is_letters(tok: str) -> bool:
    """Letters-only token. Only ever reached for pure-ASCII tokens: anything carrying a
    character of another script was already accepted by _has_other_script, so an ASCII
    test is exact here — and ABAP can express it as `CA` against a literal."""
    return all(c in _ASCII_LETTERS or c in "'-" for c in tok) and any(c in _ASCII_LETTERS for c in tok)


def _latin_word_ok(tok: str) -> bool:
    """Letters-only LATIN token: needs a vowel unless it is a short acronym; no case jumble."""
    letters = [c for c in tok if c.isalpha()]
    if len(letters) < 3:
        return True
    if ocr._CASE_JUMBLE.search(tok):
        return False
    if tok.isupper() and len(letters) <= 6:
        return True                      # HSBC, BBVA, IBAN, SWIFT
    vowels = sum(1 for c in tok.lower() if c in _VOWELS)
    return vowels >= 1


def _wellformed(tok: str) -> bool:
    """A token shaped like something a document can legitimately contain."""
    core = _strip_edges(tok)
    if not core:
        return True                      # pure punctuation (bullets, dashes, brackets)
    if _has_other_script(core):          # any other script, also glued to digits/latin (第3条, 〒123, ул.5)
        return True
    if _WEIRD_INSIDE.search(core):
        # symbols are fine only as the whole token (currency signs, ©) — never mid-word
        return len(core) == 1
    if _is_letters(core):
        if re.fullmatch(r"[A-Za-zÀ-ɏ'’\-·]+", core):
            return _latin_word_ok(core)
        return True                      # other scripts: letters are letters
    if _NUM_TOKEN.match(core):
        return True
    if _MIXED_OK.match(core):            # W9, 3a, C24, 1st, A4, 12th
        return True
    # e-mails / URLs
    if re.match(r"^[\w.+-]+@[\w.-]+$", core) or re.match(r"^(?:https?://|www\.)\S+$", core):
        return True
    # IBAN chunks, SWIFT, invoice ids: uppercase/digits with separators, >= 2 digits
    if _UPPER_CODE.match(core) and sum(c.isdigit() for c in core) >= 2:
        return True
    return False


def _short_junk(tok: str) -> bool:
    """A 1-2 char token that is neither a word, a number, punctuation nor CJK —
    mojibake is full of them (zt, q=, ;H, LI, 'J)."""
    core = _strip_edges(tok)
    if len(core) == 0 or len(core) > 2:
        return False
    if core.isdigit() or _has_other_script(core):
        return False
    if len(core) == 1:
        return not core.isalpha()        # a single letter is fine (initials, list markers)
    return core.lower() not in _SHORT_OK and not core.isupper()   # 2-letter caps = acronym/state


def features(text: str) -> dict:
    toks = (text or "").split()
    if not toks:
        return {"tokens": 0, "wellformed": 0.0, "improbable": 0.0, "symbol_density": 0.0,
                "vowel_ok": 0.0, "cjk_frac": 0.0, "short_junk": 0.0}
    wf = sum(1 for t in toks if _wellformed(t)) / len(toks)
    latin_words = [w for w in re.findall(r"[A-Za-zÀ-ɏ]{3,}", text) ]
    improbable = (sum(1 for w in latin_words if ocr._word_improbable(w)) / len(latin_words)) \
        if latin_words else 0.0
    nonspace = [c for c in text if not c.isspace()]
    sym = sum(1 for c in nonspace if c in _SYMBOLS)
    symbol_density = sym / max(1, len(nonspace))
    # vowel share over latin letters (real language ~35-50%; mojibake drifts low/high)
    latin_letters = [c.lower() for c in text if ("A" <= c <= "Z") or ("a" <= c <= "z") or ("À" <= c <= "ɏ")]
    if len(latin_letters) >= 20:
        vr = sum(1 for c in latin_letters if c in _VOWELS) / len(latin_letters)
        vowel_ok = 1.0 if 0.28 <= vr <= 0.62 else max(0.0, 1.0 - abs(vr - 0.45) * 4)
    else:
        vowel_ok = 1.0                    # too little latin to judge — do not penalise CJK pages
    cjk_chars = ocr.cjk_char_count(text)
    cjk_frac = cjk_chars / max(1, len(nonspace))
    short_junk = sum(1 for t in toks if _short_junk(t)) / len(toks)
    return {"tokens": len(toks), "wellformed": wf, "improbable": improbable,
            "symbol_density": symbol_density, "vowel_ok": vowel_ok, "cjk_frac": cjk_frac,
            "short_junk": short_junk}


def plausibility(text: str) -> float:
    f = features(text)
    if f["tokens"] == 0:
        return 0.0
    score = (0.40 * f["wellformed"]
             + 0.20 * (1.0 - f["improbable"])
             + 0.15 * (1.0 - min(1.0, f["symbol_density"] * 10))
             + 0.10 * f["vowel_ok"]
             + 0.15 * (1.0 - min(1.0, f["short_junk"] * 4)))
    return round(max(0.0, min(1.0, score)), 3)


def score_milli(text: str) -> int:
    """plausibility() as an integer 0..1000 — the unit both sides compare in.
    The ABAP twin has no floating-point API (the package computes only TYPE i), so
    parity is asserted on this integer, not on a float."""
    return int(round(plausibility(text) * 1000))


def layer_usable(text: str, min_chars: int = 40) -> tuple[bool, str]:
    """(usable, reason) for a page's text layer."""
    t = (text or "").strip()
    if len(t) < min_chars:
        return False, f"text layer has {len(t)} chars"
    s = plausibility(t)
    if s < TRUST_LAYER:
        f = features(t)
        return False, (f"text layer implausible: score {s:.2f} "
                       f"({int((1 - f['wellformed']) * 100)}% malformed tokens, "
                       f"symbol density {f['symbol_density']:.2f})")
    return True, f"text layer plausible: score {s:.2f}"
