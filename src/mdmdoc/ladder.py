#!/usr/bin/env python3
"""
ladder.py — the evidence ladder (P6): ONE bounded second perception pass when
the first extraction still misses CRITICAL fields and unread pages show
deterministic evidence they can close the gap.

Why here and not in stage_b: stage_b is the frozen trainable contract (prompt +
few-shot + LoRA); it must never re-perceive. The pipeline owns the controller
loop: perceive -> extract -> (ladder?) -> rules. The real Altrum failure — the
strong tier re-prompting the SAME two garbage pages while page 3 held every
identifier — is exactly the loop this closes.

Guarantees:
  * clean documents pay ZERO (no critical gap -> no trigger);
  * at most `ladder_pages()` extra pages, at most `ladder_vision_cap()` vision
    calls, one re-extract; never starts past `time_budget_s()`;
  * never blanks a first-pass value (stage_b._merge_tiers semantics);
  * kill switch MDMDOC_LADDER=0.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config, ocr, stage_a, stage_b
from .fields import BANK_KEYS, W9_KEYS

# escalation reasons a ladder climb is allowed to spend budget on — all of them
# name a missing CRITICAL field (identity of the money path / the taxpayer)
LADDER_CRITICAL = frozenset({
    "bank-no-account-id", "us-bank-no-routing", "bank-no-holder",
    "bank-no-bank-name", "w9-no-tin", "w9-no-line1",
})


def gap_bonus(text: str, gaps: set) -> int:
    """Deterministic evidence that THIS page can fill a SPECIFIC open gap —
    regex ID hits, holder labels, bank-letter/W-9 page markers, boxed TIN.
    Zero means the page offers nothing for what we're missing."""
    from . import fields
    t = text or ""
    if not t.strip():
        return 0
    hits = ocr.regex_fields(t)
    b = 0
    if "bank-no-account-id" in gaps and ("iban" in hits or "account_number" in hits):
        b += 3
    if "us-bank-no-routing" in gaps and "routing_aba" in hits:
        b += 3
    if "bank-no-holder" in gaps and stage_a._holder_labels_present(t):
        b += 2
    if "bank-no-bank-name" in gaps and (fields.page_markers(t)["bank_letter"]
                                        or "swift_bic" in hits):
        b += 2
    if "w9-no-tin" in gaps and ("ein" in hits or fields.find_boxed_tin(t)[0]):
        b += 3
    if "w9-no-line1" in gaps and fields.page_markers(t)["w9_form"]:
        b += 1
    return b


def climb(path: Path, raw, ext, render_dir: Path, *, t0: float, quality: bool,
          policy: str, engine: str, use_vision: bool):
    """-> (extraction, meta). Returns the original extraction untouched unless
    every trigger condition holds AND the enrichment actually read new pages."""
    meta: dict = {"used": False}
    if not config.ladder_enabled():
        return ext, meta
    if raw.ext != ".pdf" or raw.locked or raw.editable:
        return ext, meta
    gaps = set(ext.escalated_because or []) & LADDER_CRITICAL
    if not gaps:
        return ext, meta
    if time.time() - t0 >= config.time_budget_s():
        meta["skipped"] = "time-budget"
        return ext, meta
    read = set(raw.pages_used)
    unread = [i for i in sorted(raw.survey_texts) if i not in read]
    if not unread:
        return ext, meta
    ranked = sorted(((gap_bonus(raw.survey_texts.get(i) or "", gaps), i)
                     for i in unread), key=lambda x: (-x[0], x[1]))
    picks = [i for b, i in ranked if b >= 1][:config.ladder_pages()]
    if not picks:
        return ext, meta

    stats = stage_a.deep_read_extra_pages(path, raw, render_dir, picks,
                                          use_vision=use_vision,
                                          vision_cap=config.ladder_vision_cap())
    if not stats["pages"]:
        return ext, meta

    # ONE re-extract over the enriched perception; the guard chain inside
    # extract is idempotent. Merge = first pass is "fast", re-read is "strong":
    # the re-read fills gaps and wins disagreements (with a note) but can never
    # blank a first-pass value.
    ext2 = stage_b.extract(raw, quality=quality, policy=policy, engine=engine)
    keys = BANK_KEYS if ext.doc_class == "bank" else W9_KEYS
    merged, notes = stage_b._merge_tiers(ext.fields, ext2.fields, keys, policy)
    ext2.fields = merged
    for k, v in merged.items():   # provenance for values the merge restored
        filled = isinstance(v, bool) or str(v or "").strip()
        if filled and k not in ext2.provenance and k in ext.provenance:
            ext2.provenance[k] = ext.provenance[k]
    ext2.warnings.extend(notes)
    pages_h = [p + 1 for p in stats["pages"]]
    ext2.warnings.append(
        f"evidence ladder: deep-read page(s) {', '.join(map(str, pages_h))} "
        f"to close {', '.join(sorted(gaps))}")
    meta.update({"used": True, "gaps": sorted(gaps), "pages": pages_h,
                 "vision_calls": stats["vision_calls"]})
    return ext2, meta
