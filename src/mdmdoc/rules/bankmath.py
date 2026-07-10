#!/usr/bin/env python3
"""
bankmath.py — pure US bank-key arithmetic shared by rule predicates and
web_enrichment (single source of truth; no I/O, no imports beyond re).

Rules and sources (see skill sap-us-bank-validate references/rulebook.json):
  RTN-LEN       a US ABA routing number is exactly 9 numeric digits
  RTN-CHECKSUM  3(d1+d4+d7) + 7(d2+d5+d8) + 1(d3+d6+d9) ≡ 0 (mod 10)
  RTN-PREFIX    first two digits in 00, 01-12, 21-32, 61-72, 80
                (62 IS valid — electronic 61-72 range; 13-20/33-60/73-79/81-99 never assigned)
"""
from __future__ import annotations

import re


def digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def checksum_valid(aba: str) -> bool:
    """Standard ABA routing checksum (mod-10 weighted 3-7-1)."""
    d = digits(aba)
    return len(d) == 9 and checksum_sum(d) % 10 == 0


def checksum_sum(aba: str) -> int:
    """The 3-7-1 weighted sum itself — exposed so findings can SHOW the arithmetic."""
    d = digits(aba)
    n = [int(c) for c in d]
    return (3 * (n[0] + n[3] + n[6]) + 7 * (n[1] + n[4] + n[7])
            + (n[2] + n[5] + n[8])) if len(n) == 9 else -1


def prefix_valid(aba: str) -> bool:
    """First-two-digit Federal Reserve routing symbol must be an assigned range."""
    d = digits(aba)
    if len(d) != 9:
        return False
    p = int(d[:2])
    return (0 <= p <= 12) or (21 <= p <= 32) or (61 <= p <= 72) or (p == 80)


def significant_digits(account: str) -> int:
    """Digits left after stripping SAP leading-zero padding. Zeros are padding,
    never a defect: judge accounts on this, not on 'contains zeros'."""
    a = str(account or "").strip()
    return len(a.lstrip("0"))


def looks_like_bic(s: str) -> bool:
    """SWIFT/BIC shape (BOFAUS3N, CITIUS33, …) — a misplaced value in a routing field."""
    return bool(re.fullmatch(r"[A-Za-z]{6}[A-Za-z0-9]{2}([A-Za-z0-9]{3})?",
                             str(s or "").strip()))
