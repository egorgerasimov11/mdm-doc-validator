"""Consensus over independent readings of one page — the offline guarantee.

No single local engine reads every vendor document without error (benchmark,
docs/EXTRACTOR_DECISION.md). What CAN be guaranteed offline is that no value
reaches SAP silently: a value is

  confirmed    read identically by >= 2 engines of DIFFERENT families — the same
               model re-read with a different render is NOT a second voice: on the
               benchmark qwen2.5vl agreed with itself on 17 wrong numbers but with
               tesseract on only 2-3 (of 804);
  checksum_ok  read by one engine only, but its own check digit holds (IBAN mod-97,
               ABA 3-7-1, EIN/SSN shape + not a known placeholder) — a misread
               digit breaks the checksum with probability ~ 1 - 1/97 for IBAN,
               ~ 0.9 for ABA, so a passing value is a strong witness on its own;
  review       everything else — shown to the operator with the page crop,
               never written.

Values are the benchmark's entity tokens: canonical digit runs (>= 4 digits) and
IBAN / SWIFT / EIN / SSN in canonical form (bench.metrics.digit_tokens/id_tokens),
which is exactly what an operator types into a bank or tax field.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..bench.metrics import digit_tokens, id_tokens
from ..fields import iban_mod97_ok

# engine family = what shares failure modes. textlayer is exact by construction
# (a digital PDF), so it counts as its own family; OCR packages are separate
# families; every vision-language model is ONE family regardless of vendor —
# they are trained on the same kind of data and hallucinate the same way.
FAMILY_OF = {"textlayer": "textlayer", "tess": "tesseract", "rapidocr": "rapidocr",
             "applevision": "applevision", "ollama": "vlm", "mlx": "vlm"}

_KNOWN_FAKE_TINS = ("123456789", "987654321", "078051120", "219099999", "000000000")


def family_of(engine_id: str) -> str:
    fam = engine_id.split(":", 1)[0].split("@", 1)[0]
    return FAMILY_OF.get(fam, fam)


def aba_checksum_ok(d: str) -> bool:
    if not re.fullmatch(r"\d{9}", d) or d in _KNOWN_FAKE_TINS:
        return False
    w = (3, 7, 1) * 3
    if sum(int(c) * k for c, k in zip(d, w)) % 10 != 0:
        return False
    return d[:2] <= "12" or "21" <= d[:2] <= "32" or "61" <= d[:2] <= "72" or d[:2] == "80"


_US_TIN_CONTEXT = re.compile(r"(?i)\bEIN\b|employer identification|taxpayer identification|\bTIN\b|"
                             r"social security|\bSSN\b|\bW-?9\b|\bW-?8")


def checksum_ok(token: str, context: str = "") -> bool:
    """A token that carries its own proof of correct reading. EIN/SSN have no check
    digit — only a shape — so the shape counts only on a page that talks about US
    tax ids: a Hungarian company register number 01-10-041043 written without one
    hyphen is a perfect EIN shape and was accepted that way once."""
    if token.startswith("IBAN:"):
        return iban_mod97_ok(token[5:])
    if token.startswith(("EIN:", "SSN:")) and not _US_TIN_CONTEXT.search(context or ""):
        return False
    if token.startswith("EIN:"):
        d = token[4:].replace("-", "")
        return d[:2] not in ("00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70",
                             "78", "79", "89") and d not in _KNOWN_FAKE_TINS
    if token.startswith("SSN:"):
        a, b, c = token[4:].split("-")
        return a not in ("000", "666") and not a.startswith("9") and b != "00" and c != "0000"
    if token.startswith("SWIFT:"):
        return False                    # no check digit; needs a second voice or a directory
    return aba_checksum_ok(token)       # bare 9-digit run that is a valid routing number


# ISO 13616 national lengths for the countries that appear in vendor bank documents;
# used to cut an IBAN the greedy regex ran past (it accepts any [A-Z0-9] run up to 34).
IBAN_LEN = {"AT": 20, "BE": 16, "BG": 22, "CH": 21, "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24,
            "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IT": 27,
            "LT": 20, "LU": 20, "LV": 21, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24,
            "SA": 24, "SE": 24, "SI": 19, "SK": 24, "TR": 26, "AE": 23, "QA": 29, "KW": 30, "BH": 22}


def canonical_iban(raw: str) -> str:
    """Canonical IBAN from a regex hit: cut to the country's length, require mod-97."""
    v = re.sub(r"\s", "", raw).upper()
    n = IBAN_LEN.get(v[:2])
    if n and len(v) > n:
        v = v[:n]
    return v                    # checksum_ok() decides the status; a bad IBAN still goes to review


# Not identifiers: money amounts (a currency sign or a decimal part) and calendar
# dates. They are read too, but they are not what an operator copies into a bank
# or tax field, and their spellings vary (5,000 vs 5.000,00; 1-1-24 vs 01/01/2024).
_AMOUNT_RE = re.compile(r"(?<![\w])[$€£¥₩]\s?\d[\d,. ]*|\b\d{1,3}(?:[.,]\d{3})*[.,]\d{2}\b(?!\d)")
_DATE_RE = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b")


def _without_amounts_and_dates(text: str) -> str:
    return _DATE_RE.sub(" ", _AMOUNT_RE.sub(" ", text or ""))


_SPACED_RUN = re.compile(r"(?<!\d)(\d[\d\-./]*(?: \d[\d\-./]*)+)(?!\d)")


def _split_table_runs(text: str) -> str:
    """A table row read by OCR — "30003 02110 00037262223 29" — is several values
    separated by single spaces, but the benchmark's digit-run regex treats a single
    space as an in-number separator and glues them into one 23-digit token, so the
    OCR voice is lost for every cell. Groups of UNEQUAL length are distinct values:
    put two spaces between them. Uniformly grouped numbers ("4830 2291 0077",
    "3000 3021 1000 0372") stay one number."""
    def fix(m):
        groups = m.group(1).split(" ")
        lens = {len(re.sub(r"\D", "", g)) for g in groups[:-1]} if len(groups) > 2 else {len(re.sub(r"\D", "", g)) for g in groups}
        if len(lens) > 1 and any(len(re.sub(r"\D", "", g)) >= 4 for g in groups):
            return "  ".join(groups)
        return m.group(1)
    return _SPACED_RUN.sub(fix, text or "")


def tokens_of(text: str) -> Counter:
    """All entity tokens of a transcript, canonical (digit runs + typed ids). A digit
    run that is just the digits of a typed id on the same page (the IBAN's body, an
    EIN without its hyphen) is dropped — it is the same value, not a second one."""
    ids = Counter()
    for tok, n in id_tokens(text).items():
        if tok.startswith("IBAN:"):
            tok = "IBAN:" + canonical_iban(tok[5:])
        ids[tok] += n
    shadows = {re.sub(r"\D", "", v.split(":", 1)[1]) for v in ids}
    out = Counter({d: n for d, n in digit_tokens(_split_table_runs(_without_amounts_and_dates(text))).items()
                   if d not in shadows})
    out.update(ids)
    return out


@dataclass
class Verdict:
    value: str
    status: str                       # confirmed | checksum_ok | review
    voices: list[str] = field(default_factory=list)   # engine ids that read it
    families: list[str] = field(default_factory=list)
    count: int = 1                    # max multiplicity over the voices

    def as_dict(self) -> dict:
        return {"value": self.value, "status": self.status, "voices": self.voices,
                "families": self.families, "count": self.count}


# Agreement of two families is not enough when the two can make the SAME mistake:
# a letter O read for a digit 0 in a BIC branch code (both tesseract and qwen2.5vl
# wrote ICRAITRRISO for ICRAITRRIS0), or one zero dropped from a run of zeros in a
# 20-digit Chinese account number (both engines lost it). Such values need a third
# family or a checksum. These are the only two agreed-wrong classes the benchmark
# produced (2 of 649 accepted values); the rule is what keeps the count at zero.
CONFUSABLE = set("O0I1l5SB8Z2")
LONG_RUN = 16                 # digits; an IBAN body or a long Asian account number
EXTRA_WITNESS_FAMILIES = 3


def needs_extra_witness(token: str) -> bool:
    kind, _, val = token.partition(":")
    if not val:                                   # bare digit run
        return len(kind) >= LONG_RUN or bool(re.search(r"0{4,}", kind))
    if kind == "IBAN":
        return False                              # mod-97 decides
    if kind in ("EIN", "SSN"):
        return False                              # digits only, short, shape-checked
    return any(c in CONFUSABLE for c in val)      # SWIFT and other alphanumerics


def consensus(readings: dict[str, str]) -> list[Verdict]:
    """readings: {engine_id: transcript of ONE page} → one verdict per distinct token,
    sorted confirmed → checksum_ok → review, then by value."""
    seen: dict[str, dict] = {}
    for eid, text in readings.items():
        if not text:
            continue
        fam = family_of(eid)
        for tok, n in tokens_of(text).items():
            slot = seen.setdefault(tok, {"voices": [], "families": set(), "count": 0})
            slot["voices"].append(eid)
            slot["families"].add(fam)
            slot["count"] = max(slot["count"], n)
    visual_voices = any(family_of(e) != "textlayer" and (t or "").strip() for e, t in readings.items())
    context = "\n".join(t or "" for t in readings.values())
    out: list[Verdict] = []
    for tok, slot in seen.items():
        need = EXTRA_WITNESS_FAMILIES if needs_extra_witness(tok) else 2
        if slot["families"] == {"textlayer"} and visual_voices:
            # the PDF text layer can carry values the page does not SHOW (an ING
            # statement with the counterparty IBANs whited out "for privacy reasons"
            # still had them in the layer). With visual engines on the page and none
            # of them seeing the value, it is not on the page — never auto-accept.
            st = "review"
        elif checksum_ok(tok, context):
            st = "checksum_ok" if len(slot["families"]) < 2 else "confirmed"
        elif len(slot["families"]) >= need:
            st = "confirmed"
        else:
            st = "review"
        out.append(Verdict(tok, st, sorted(slot["voices"]), sorted(slot["families"]), slot["count"]))
    order = {"confirmed": 0, "checksum_ok": 1, "review": 2}
    out.sort(key=lambda v: (order[v.status], v.value))
    return out


def accepted(verdicts: list[Verdict]) -> set[str]:
    """Tokens the extractor would hand over without a human: confirmed + checksum_ok."""
    return {v.value for v in verdicts if v.status != "review"}


# ── scoring against gold (benchmark) ─────────────────────────────────────────

def score_consensus(gold_text: str, readings: dict[str, str]) -> dict:
    """How the offline guarantee performs on one page: of the values the consensus
    would hand over WITHOUT a human (confirmed + checksum_ok), how many are not in
    the gold at all (silent errors — the number that must be zero), and how much of
    the gold it covers automatically (auto share). `review` values cost operator
    time but never an error."""
    from ..bench.metrics import normalize
    gold = set(tokens_of(gold_text))
    loose_gold = normalize(gold_text, "loose")
    verdicts = consensus(readings)
    acc = accepted(verdicts)

    def in_gold(tok: str) -> bool:
        if tok in gold:
            return True
        kind, _, val = tok.partition(":")
        needle = re.sub(r"\D", "", val) if kind in ("EIN", "SSN") else (val or kind).casefold()
        # a value the gold wrote with different separators (or across a line break)
        return bool(needle) and needle in loose_gold
    silent = sorted(t for t in acc if not in_gold(t))
    auto = {t for t in acc if t in gold}
    return {
        "gold_values": len(gold),
        "auto_found": len(auto),
        "auto_share": round(len(auto) / len(gold), 4) if gold else 1.0,
        "accepted": len(acc),
        "silent_errors": len(silent),
        "silent_error_values": silent,
        "review": sum(1 for v in verdicts if v.status == "review"),
        "confirmed": sum(1 for v in verdicts if v.status == "confirmed"),
        "checksum_ok": sum(1 for v in verdicts if v.status == "checksum_ok"),
    }
