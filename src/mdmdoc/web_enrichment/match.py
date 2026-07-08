#!/usr/bin/env python3
"""
match.py — the ONE name matcher for every registry connector.

Before this module each connector had its own matcher and FDIC accepted ANY
single shared non-stopword token — "First National Bank" matched unrelated
"First …" institutions and the panel showed a confident FOUND with a wrong
city/state. A false FOUND/CONFLICT can never move the verdict (NOTE-only), but
it CAN mislead the operator, so the matcher errs strict:

  * substring either way after normalization, OR
  * ≥2 shared meaningful tokens (legal-form words and generic bank words are
    not meaningful).

`best_match` replaces "take the first registry row": among matching candidates
it picks the one sharing the MOST meaningful tokens with the document name.
"""
from __future__ import annotations

from ..fields import _norm_name

STOP = {"bank", "na", "n", "a", "the", "of", "and", "trust", "company", "co",
        "usa", "inc", "llc", "ltd", "corp", "corporation", "gmbh", "sa", "srl",
        "spa", "nv", "bv", "plc", "ag", "kg", "pte", "sas", "banco", "banca"}


def name_matches(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    core = (set(na.split()) & set(nb.split())) - STOP
    return len(core) >= 2


def best_match(name: str, candidates: list, key=lambda c: c):
    """The matching candidate with the largest meaningful-token overlap, or
    None. Never returns a non-matching candidate."""
    tn = set(_norm_name(name).split()) - STOP
    scored = []
    for c in candidates:
        if not name_matches(name, key(c)):
            continue
        cn = set(_norm_name(key(c)).split()) - STOP
        scored.append((len(tn & cn), c))
    if not scored:
        return None
    return max(scored, key=lambda t: t[0])[1]
