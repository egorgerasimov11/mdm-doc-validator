"""Transcription-quality metrics (gold vs candidate).

Headline metrics are ORDER-INSENSITIVE — reading order is the noisiest part of
any transcription and must not decide the benchmark:

  field_value_recall  every gold field value (label→value pair Claude listed)
                      must occur in the candidate text — the "not one character
                      lost" criterion on exactly what an operator types into SAP
  entity_recall       multiset recall of canonical digit tokens (>= 4 digits,
                      separators removed) and bank/tax ids (IBAN, SWIFT, EIN, SSN,
                      ABA) — account numbers, phones, dates, amounts, references
  line_recall         fuzzy 1:1 line alignment (rapidfuzz ratio >= 85)
  cer / wer           normalized character / word error rate (secondary; WER is
                      suppressed when the gold is mostly CJK)

Normalization (`normalize`): NFKC, unified quotes/dashes, transcription markup
stripped ([hw]…[/hw], [seal: …], [signature], …), markdown table pipes → spaces,
CJK inter-glyph spaces collapsed, whitespace squeezed. `loose` additionally
casefolds, maps Arabic-Indic digits to ASCII and drops punctuation and spaces.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from .. import ocr

# ── normalization ─────────────────────────────────────────────────────────────

_QUOTES = {"“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‟": '"', "″": '"',
           "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'", "´": "'"}
_DASHES = {"–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", "‒": "-", "―": "-", "ー": "ー"}
_MARKUP_RE = re.compile(
    r"\[/?hw\]|\[(?:seal|stamp|signature|sign|logo|photo|image|barcode|qr ?code|"
    r"checkbox|handwritten|illegible|unreadable|watermark|table|figure|chart|icon)[^\]]*\]",
    re.IGNORECASE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_ARABIC_INDIC = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_ARABIC_INDIC.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_PUNCT_RE = re.compile(r"[^\w]|_", re.UNICODE)


def strip_markup(text: str) -> str:
    """Remove transcription annotations so that marking conventions never count as errors."""
    t = _MARKUP_RE.sub(" ", text or "")
    t = t.replace("☑", " [x] ").replace("☐", " [ ] ").replace("✓", " [x] ").replace("✔", " [x] ")
    return t


def normalize(text: str, level: str = "base") -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = strip_markup(t)
    for k, v in _QUOTES.items():
        t = t.replace(k, v)
    for k, v in _DASHES.items():
        t = t.replace(k, v)
    t = t.replace("…", "...")
    lines = []
    for line in t.split("\n"):
        if _TABLE_SEP_RE.match(line):
            continue
        line = line.replace("|", " ")
        line = ocr.collapse_cjk_spaces(line)
        line = re.sub(r"[ \t 　]+", " ", line).strip()
        if line:
            lines.append(line)
    out = "\n".join(lines)
    if level == "loose":
        out = out.casefold().translate(_ARABIC_INDIC)
        out = _PUNCT_RE.sub("", out)
    return out


def lines_of(text: str, level: str = "base") -> list[str]:
    return [ln for ln in normalize(text, level).split("\n") if ln]


_CJK_ANY_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿぀-ヿㇰ-ㇿ가-힣ᄀ-ᇿ㄰-㆏]")


def cjk_fraction(text: str) -> float:
    """Share of Han/Kana/Hangul glyphs among non-space characters."""
    t = re.sub(r"\s+", "", text or "")
    return len(_CJK_ANY_RE.findall(t)) / max(1, len(t))


# ── character / word error rates ──────────────────────────────────────────────

def cer(gold: str, cand: str, level: str = "base") -> float:
    g = normalize(gold, level).replace("\n", " ")
    c = normalize(cand, level).replace("\n", " ")
    if not g:
        return 0.0 if not c else 1.0
    return round(Levenshtein.distance(g, c) / len(g), 4)


def wer(gold: str, cand: str) -> float | None:
    """None when the gold is mostly CJK (unspaced scripts make WER meaningless)."""
    if cjk_fraction(gold) > 0.30:
        return None
    g = normalize(gold).replace("\n", " ").split()
    c = normalize(cand).replace("\n", " ").split()
    if not g:
        return 0.0 if not c else 1.0
    return round(Levenshtein.distance(g, c) / len(g), 4)


# ── line alignment ────────────────────────────────────────────────────────────

@dataclass
class LineAlign:
    recall: float
    precision: float
    matched: list[tuple[int, int, float]] = field(default_factory=list)
    unmatched_gold: list[str] = field(default_factory=list)
    unmatched_cand: list[str] = field(default_factory=list)


def line_align(gold_lines: list[str], cand_lines: list[str], cutoff: float = 85.0) -> LineAlign:
    """Greedy one-to-one fuzzy matching of normalized lines."""
    if not gold_lines:
        return LineAlign(1.0, 1.0 if not cand_lines else 0.0, [], [], list(cand_lines))
    if not cand_lines:
        return LineAlign(0.0, 1.0, [], list(gold_lines), [])
    g = [normalize(x, "loose") or normalize(x) for x in gold_lines]
    c = [normalize(x, "loose") or normalize(x) for x in cand_lines]
    # plain double loop (pages have at most a few hundred lines; no numpy dependency)
    pairs = []
    for i, gi in enumerate(g):
        for j, cj in enumerate(c):
            s = fuzz.ratio(gi, cj, score_cutoff=cutoff)
            if s >= cutoff:
                pairs.append((float(s), i, j))
    pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_g: set[int] = set()
    used_c: set[int] = set()
    matched: list[tuple[int, int, float]] = []
    for s, i, j in pairs:
        if i in used_g or j in used_c:
            continue
        used_g.add(i)
        used_c.add(j)
        matched.append((i, j, s))
    um_g = [gold_lines[i] for i in range(len(g)) if i not in used_g]
    um_c = [cand_lines[j] for j in range(len(c)) if j not in used_c]
    return LineAlign(round(len(matched) / len(g), 4), round(len(matched) / len(c), 4),
                     sorted(matched), um_g, um_c)


# ── entities ──────────────────────────────────────────────────────────────────

_DIGIT_RUN_RE = re.compile(r"(?<![\d])(\d(?:\d|[ \-./,:](?=\d)){3,})(?![\d])")
_ABA_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")


def digit_tokens(text: str) -> Counter:
    """Canonical digit runs (>= 4 digits; internal separators removed)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_ARABIC_INDIC)
    out: Counter = Counter()
    for m in _DIGIT_RUN_RE.finditer(t):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) >= 4:
            out[digits] += 1
    return out


def id_tokens(text: str) -> Counter:
    """IBAN / SWIFT / EIN / SSN found by the validator's own regexes (canonical forms)."""
    t = unicodedata.normalize("NFKC", text or "")
    out: Counter = Counter()
    for m in ocr.IBAN_RE.finditer(t):
        v = re.sub(r"\s", "", m.group(1)).upper()
        if 15 <= len(v) <= 34:
            out["IBAN:" + v] += 1
    for m in ocr.SWIFT_RE.finditer(t):
        v = m.group(1).upper()
        if not v.isalpha() or re.search(r"(?i)(?:bic|swift)[^\n]{0,20}" + re.escape(v), t):
            out["SWIFT:" + v] += 1
    for m in ocr.EIN_RE.finditer(t):
        out["EIN:" + m.group(1)] += 1
    for m in ocr.SSN_RE.finditer(t):
        out["SSN:" + m.group(1)] += 1
    return out


@dataclass
class Recall:
    recall: float
    total: int
    found: int
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"recall": self.recall, "total": self.total, "found": self.found,
                "missing": self.missing[:50]}


def _multiset_recall(gold: Counter, cand: Counter) -> Recall:
    total = sum(gold.values())
    if total == 0:
        return Recall(1.0, 0, 0, [])
    found = 0
    missing: list[str] = []
    for k, n in gold.items():
        have = min(n, cand.get(k, 0))
        found += have
        if have < n:
            missing.extend([k] * (n - have))
    return Recall(round(found / total, 4), total, found, missing)


def entity_recall(gold_text: str, cand_text: str) -> Recall:
    g = digit_tokens(gold_text) + id_tokens(gold_text)
    c = digit_tokens(cand_text) + id_tokens(cand_text)
    # an id the candidate wrote without its label (plain "NACFKRSE" with no "SWIFT")
    # is still present: rescue by loose substring on the canonical value
    loose = normalize(cand_text, "loose")
    for key, n in g.items():
        if ":" in key and c.get(key, 0) < n:
            kind, val = key.split(":", 1)
            needle = re.sub(r"\D", "", val) if kind in ("EIN", "SSN") else val.casefold()
            if needle and loose.count(needle) > c.get(key, 0):
                c[key] = max(c.get(key, 0), min(n, loose.count(needle)))
    return _multiset_recall(g, c)


# ── field values ──────────────────────────────────────────────────────────────

def _value_present(value: str, cand_base: str, cand_loose: str) -> bool:
    vb = normalize(value).replace("\n", " ")
    if not vb:
        return True
    if vb in cand_base:
        return True
    vl = normalize(value, "loose")
    return bool(vl) and vl in cand_loose


def field_value_recall(gold_fields: list[dict], cand_text: str) -> tuple[Recall, Recall]:
    """(all fields, handwritten-only). A value counts as found when its normalized
    form occurs in the candidate text (base), or its loose form (no spaces /
    punctuation / case) occurs in the loose candidate text."""
    cand_base = normalize(cand_text).replace("\n", " ")
    cand_loose = normalize(cand_text, "loose")
    tot = found = 0
    htot = hfound = 0
    missing: list[str] = []
    hmissing: list[str] = []
    for f in gold_fields or []:
        v = str(f.get("value", "") or "").strip()
        if len(normalize(v, "loose")) < 2 or v in ("☑", "☐", "[x]", "[ ]"):
            continue
        ok = _value_present(v, cand_base, cand_loose)
        tot += 1
        found += int(ok)
        label = str(f.get("label", "") or "")
        if not ok:
            missing.append(f"{label}: {v}" if label else v)
        if f.get("handwritten"):
            htot += 1
            hfound += int(ok)
            if not ok:
                hmissing.append(f"{label}: {v}" if label else v)
    allr = Recall(round(found / tot, 4) if tot else 1.0, tot, found, missing)
    hwr = Recall(round(hfound / htot, 4) if htot else 1.0, htot, hfound, hmissing)
    return allr, hwr


# ── page / document scoring ───────────────────────────────────────────────────

def score_page(gold_text: str, gold_fields: list[dict], cand_text: str) -> dict:
    gl = lines_of(gold_text)
    cl = lines_of(cand_text)
    la = line_align(gl, cl)
    er = entity_recall(gold_text, cand_text)
    fr, hr = field_value_recall(gold_fields, cand_text)
    gold_chars = len(normalize(gold_text).replace("\n", ""))
    return {
        "cer": cer(gold_text, cand_text),
        "cer_loose": cer(gold_text, cand_text, "loose"),
        "wer": wer(gold_text, cand_text),
        "line_recall": la.recall,
        "line_precision": la.precision,
        "unmatched_gold_lines": la.unmatched_gold[:40],
        "entity": er.as_dict(),
        "field": fr.as_dict(),
        "field_hw": hr.as_dict(),
        "gold_chars": gold_chars,
        "cand_chars": len(normalize(cand_text).replace("\n", "")),
        "cjk_fraction": round(cjk_fraction(gold_text), 3),
        "empty": not normalize(cand_text).strip(),
    }


def aggregate_pages(pages: list[dict]) -> dict:
    """Document-level numbers: CER weighted by gold length; recalls pooled."""
    pages = [p for p in pages if p]
    if not pages:
        return {}
    w = sum(p["gold_chars"] for p in pages) or 1
    def pooled(key):
        tot = sum(p[key]["total"] for p in pages)
        found = sum(p[key]["found"] for p in pages)
        return round(found / tot, 4) if tot else 1.0, tot
    ent, ent_n = pooled("entity")
    fld, fld_n = pooled("field")
    hw, hw_n = pooled("field_hw")
    wers = [p["wer"] for p in pages if p["wer"] is not None]
    return {
        "pages": len(pages),
        "cer": round(sum(p["cer"] * p["gold_chars"] for p in pages) / w, 4),
        "cer_loose": round(sum(p["cer_loose"] * p["gold_chars"] for p in pages) / w, 4),
        "wer": round(sum(wers) / len(wers), 4) if wers else None,
        "line_recall": round(sum(p["line_recall"] * p["gold_chars"] for p in pages) / w, 4),
        "entity_recall": ent, "entity_total": ent_n,
        "field_recall": fld, "field_total": fld_n,
        "field_hw_recall": hw, "field_hw_total": hw_n,
        "empty_pages": sum(1 for p in pages if p["empty"]),
    }


def aggregate_docs(docs: list[dict]) -> dict:
    """Slice-level: macro average over documents + the worst document per metric."""
    docs = [d for d in docs if d]
    if not docs:
        return {}
    def macro(key):
        vals = [d[key] for d in docs if d.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    def worst(key, lower_is_better=False):
        vals = [(d[key], d.get("doc_id", "?")) for d in docs if d.get(key) is not None]
        if not vals:
            return None, None
        v = max(vals) if lower_is_better else min(vals)
        return v
    out = {"docs": len(docs)}
    for key in ("cer", "cer_loose", "wer", "line_recall", "entity_recall", "field_recall",
                "field_hw_recall"):
        out[key] = macro(key)
        wv = worst(key, lower_is_better=key.startswith("cer") or key == "wer")
        out[key + "_worst"] = wv[0] if wv else None
        out[key + "_worst_doc"] = wv[1] if wv else None
    out["empty_pages"] = sum(d.get("empty_pages", 0) for d in docs)
    return out


# ── thresholds / decision ─────────────────────────────────────────────────────

THRESHOLDS = {
    # slice kind → (field_recall_worst, entity_recall_worst, cer_macro, line_recall_macro)
    "print": {"field_recall_worst": 1.0, "entity_recall_worst": 0.995, "cer": 0.01, "line_recall": 0.98},
    "photo": {"field_recall_worst": 1.0, "entity_recall_worst": 0.995, "cer": 0.02, "line_recall": 0.98},
    "handwriting": {"field_recall_worst": 0.95, "entity_recall_worst": 0.95, "cer": 0.05, "line_recall": 0.90},
}


def passes(slice_kind: str, agg: dict) -> tuple[bool, list[str]]:
    th = THRESHOLDS.get(slice_kind, THRESHOLDS["print"])
    fails = []
    if (agg.get("field_recall_worst") or 0) < th["field_recall_worst"]:
        fails.append(f"field_recall(worst doc) {agg.get('field_recall_worst')} < {th['field_recall_worst']}")
    if (agg.get("entity_recall_worst") or 0) < th["entity_recall_worst"]:
        fails.append(f"entity_recall(worst doc) {agg.get('entity_recall_worst')} < {th['entity_recall_worst']}")
    if (agg.get("cer") if agg.get("cer") is not None else 1) > th["cer"]:
        fails.append(f"cer {agg.get('cer')} > {th['cer']}")
    if (agg.get("line_recall") or 0) < th["line_recall"]:
        fails.append(f"line_recall {agg.get('line_recall')} < {th['line_recall']}")
    return (not fails), fails
