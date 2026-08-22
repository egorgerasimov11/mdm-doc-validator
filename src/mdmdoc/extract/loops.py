"""Degenerate-repetition ("loop") detection for VLM transcripts.

A vision model that loses the page sometimes emits one table cell or one line
over and over until it hits the token limit: qwen2.5vl:7b produced 10 239
characters of a single cell on a French RIB (279 s, zero values extracted).
The benchmark must see that, and the extractor must never hand such a page to
an operator as if it were a transcription.

The detector is STRUCTURAL, not statistical. A naive n-gram-uniqueness threshold
cannot be used: genuine pages exist with 8-gram uniqueness 0.08 (a statement that
is mostly "REDACTED") and 0.20 (a RIB that prints the same bank details twice).
Calibrated on 116 gold pages + 246 normal VLM pages (0 false positives) and 33
looped pages (32 caught; the miss had a repeat unit longer than 200 chars, hence
MAX_UNIT = 400).

Rules (any one fires):
  1. a line longer than LONG_LINE chars whose body is one unit of MIN_UNIT..MAX_UNIT
     chars repeated >= MIN_REPEATS times consecutively;
  2. >= MIN_REPEATS identical CONTENT lines in a row — empty table rows such as
     `|  |  |  |` do not count (two genuine W-8BEN-E pages have those);
  3. one content line of >= MIN_UNIT chars making up more than DOMINANT_SHARE of
     all content lines on the page;
  4. a BLOCK of 2..MAX_BLOCK content lines repeated >= BLOCK_MIN_REPEATS times in a
     row (the RIB case: the model re-emitted the whole 25-line slip twelve times, so
     no single line dominated and no line repeated consecutively). The threshold is
     higher than MIN_REPEATS because a genuine French RIB sheet carries SIX identical
     detachable slips — that page is in the gold set and must not fire.
"""
from __future__ import annotations

import re

MIN_UNIT = 12
MAX_UNIT = 400
MIN_REPEATS = 6
LONG_LINE = 1500
DOMINANT_SHARE = 0.30
MIN_DOMINANT_LINES = 10          # rule 3 needs a page, not a four-line stub
MAX_BLOCK = 80                   # rule 4: longest block period considered
BLOCK_MIN_REPEATS = 8            # rule 4: a real RIB sheet has 6 copies; loops have 12+

_EMPTY_TABLE_ROW = re.compile(r"^[|\s:\-+]*$")
_INLINE_LOOP = re.compile(r"(.{%d,%d}?)(?:\s*\1){%d,}" % (MIN_UNIT, MAX_UNIT, MIN_REPEATS - 1), re.DOTALL)


def _content_key(line: str) -> str | None:
    """Normalised line, or None when the line carries no content (blank, table
    scaffolding, separators)."""
    s = re.sub(r"\s+", " ", line.strip())
    if not s or _EMPTY_TABLE_ROW.match(s):
        return None
    return s


def looks_looped(text: str) -> tuple[bool, str]:
    """→ (looped?, reason). Empty reason when the text looks like a transcription."""
    if not text:
        return False, ""
    lines = text.split("\n")
    # rule 1: one very long line built from a repeated unit
    for ln in lines:
        if len(ln) > LONG_LINE and _INLINE_LOOP.search(ln):
            return True, f"inline repeat in a {len(ln)}-char line"
    keys = [_content_key(ln) for ln in lines]
    content = [k for k in keys if k]
    # rule 2: the same content line >= MIN_REPEATS times in a row
    run_key, run = None, 0
    for k in keys:
        if k is None:
            continue
        if k == run_key:
            run += 1
            if run >= MIN_REPEATS:
                return True, f"line repeated {run}x consecutively: {k[:60]!r}"
        else:
            run_key, run = k, 1
    # rule 3: one line dominates the page
    if len(content) >= MIN_DOMINANT_LINES:
        counts: dict[str, int] = {}
        for k in content:
            counts[k] = counts.get(k, 0) + 1
        k, n = max(counts.items(), key=lambda kv: kv[1])
        if len(k) >= MIN_UNIT and n / len(content) > DOMINANT_SHARE:
            return True, f"line is {n}/{len(content)} of the page: {k[:60]!r}"
    # rule 4: a block of lines repeated periodically
    period, copies = _block_period(content)
    if period:
        return True, f"block of {period} lines repeated {copies}x"
    return False, ""


def _block_period(content: list[str]) -> tuple[int, int]:
    """Smallest period p (2..MAX_BLOCK) for which a lag-p match run covers at least
    BLOCK_MIN_REPEATS copies → (p, copies); (0, 0) when there is none."""
    n = len(content)
    for period in range(2, min(MAX_BLOCK, n // BLOCK_MIN_REPEATS) + 1):
        run = best = 0
        for i in range(n - period):
            if content[i] == content[i + period]:
                run += 1
                best = max(best, run)
            else:
                run = 0
        # `best` lag matches span best+period lines = best/period + 1 copies
        if best + period >= BLOCK_MIN_REPEATS * period:
            return period, best // period + 1
    return 0, 0


def collapse_repeats(text: str) -> str:
    """Last resort when every re-read still loops: keep ONE copy of each repeated
    run so the page at least carries what the model did read. Consecutive identical
    content lines collapse to one; a periodically repeated block keeps its first
    copy; inside long lines a unit repeated >= MIN_REPEATS times collapses to one.
    Only ever applied on the retry path — normal pages are returned byte-for-byte
    untouched."""
    out: list[str] = []
    prev_key = None
    for ln in (text or "").split("\n"):
        if len(ln) > 200:
            ln = _INLINE_LOOP.sub(r"\1", ln)
        k = _content_key(ln)
        if k is not None and k == prev_key:
            continue
        out.append(ln)
        if k is not None:
            prev_key = k
    keyed = [(ln, _content_key(ln)) for ln in out]
    content = [k for _, k in keyed if k]
    period, _ = _block_period(content)
    if period:
        # drop every content line that merely repeats the line `period` content-lines
        # back, once the periodic run has started
        kept: list[str] = []
        seen_content: list[str] = []
        for ln, k in keyed:
            if k is not None:
                i = len(seen_content)
                seen_content.append(k)
                if i >= period and k == seen_content[i - period]:
                    continue
            kept.append(ln)
        out = kept
    return "\n".join(out)
