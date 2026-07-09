#!/usr/bin/env python3
"""
doctype_evidence.py — deterministic doc-type evidence scorer (П3, R1).

Problem: the blunt `ground_payment_instructions` guard sends every ungrounded
model guess to other→BNK-030 NMR. That is SAFE but noisy: a real LV/EU bank
letter the model mislabels payment_instructions lands in manual review even
though the page carries overwhelming deterministic bank-letter evidence
(letter phrasing + bank identity + account facts + an addressee/holder line).

This scorer is a PURE, table-driven function over signals stage A already
extracted. The rescue path (stage_b._ground_payment_instructions) uses it in
the SAFE direction only: strong evidence reclassifies to bank_letter WITH a
`doc_type_uncertain` flag (a confidence weak signal — CONF-001 can still hold
a shaky ACCEPT); weak/ambiguous evidence keeps today's other→NMR behavior.
Weak evidence can never upgrade: the rescue target still passes through
BNK-021/023/024/025, and true payment instructions carry payment marks so the
guard exits before the scorer is even consulted.

Table-driven and model-free so the ABAP twin can port it later (tracked in
PARITY.md Coordination requests; ABAP keeps the stricter grounding meanwhile —
a safe temporary divergence).
"""
from __future__ import annotations

import re

from .fields import (_BANK_LETTER_PHRASES, page_markers,
                     payment_instruction_marks)

# bank-identity: a NAMED bank, not the bare word 'bank' (a heading like
# "Bank confirmation letter" is generic — 'Swedbank', 'Danske Bank',
# 'Bancolombia', '某某银行' are identities). See _bank_identity().
_BANK_TOKENS = ("bank", "banka", "banca", "banco", "banque", "sparkasse",
                "sporitelna", "spořitelna")
_BANK_CONNECTIVES = ("of", "de", "di", "du", "der")

# addressee / greeting shapes at line starts — a letter is WRITTEN TO someone
_GREETING_SHAPES = ("dear ", "to whom it may concern", "re:", "subject:",
                    "a quien corresponda", "sehr geehrte")

# holder labels (mirror of stage_a._holder_labels_present's intent, kept local
# so this module stays import-light and ABAP-portable)
_HOLDER_LABELS = ("account holder", "beneficiary", "account name",
                  "in the name of", "intestatario", "titular", "kontoinhaber",
                  "holder:", "we confirm that", "confirm that")

_STATEMENT_MARKS = ("statement period", "opening balance", "closing balance",
                    "statement of account", "account statement")

# the AND-gate: EVERY component must hold for a bank_letter rescue
THRESHOLDS = {"bank_letter": {"require": ("letter_shape", "bank_identity",
                                          "account_facts", "holder_signal")}}


def _bank_identity(text: str, cands: dict) -> bool:
    """A NAMED bank is present: a SWIFT candidate, a CJK bank ideogram, a
    fused bank name (Swedbank, Bancolombia), or a bank token flanked by a
    capitalized name word on its line ('Danske Bank', 'Bank of America').
    The bare heading word 'Bank …' does NOT count."""
    if str(cands.get("swift_bic") or "").strip():
        return True
    if "银行" in (text or "") or "銀行" in (text or ""):
        return True
    tok_re = re.compile(r"(?i)[\w&.\-']*(?:" + "|".join(_BANK_TOKENS) + r")[\w&.\-']*")
    for line in (text or "").splitlines():
        for m in tok_re.finditer(line):
            word = m.group(0)
            if not word[:1].isupper():
                continue
            if word.lower() not in _BANK_TOKENS:
                return True                     # fused proper name (Swedbank)
            toks = line.split()
            try:
                i = next(j for j, w in enumerate(toks) if word in w)
            except StopIteration:
                continue
            if i > 0 and toks[i - 1][:1].isupper():
                return True                     # 'Danske Bank'
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if nxt[:1].isupper():
                return True                     # 'Bank Pekao'
            if (nxt.lower() in _BANK_CONNECTIVES and i + 2 < len(toks)
                    and toks[i + 2][:1].isupper()):
                return True                     # 'Bank of America'
    return False


def score(text: str, regex_candidates: dict | None,
          bank_letter_pages: list | None) -> dict:
    """-> {'components': {...bool...}, 'bank_letter_strong': bool}.
    Pure function over already-extracted signals — no model, no I/O."""
    t = (text or "").lower()
    cands = regex_candidates or {}

    letter_shape = (any(p in t for p in _BANK_LETTER_PHRASES)
                    or bool(page_markers(text or "").get("bank_letter"))
                    or bool(bank_letter_pages))
    bank_identity = _bank_identity(text or "", cands)
    account_facts = any(str(cands.get(k) or "").strip()
                        for k in ("iban", "account_number", "routing_aba"))
    holder_signal = (any(lbl in t for lbl in _HOLDER_LABELS)
                     or any(re.search(rf"(?im)^\s*{re.escape(g)}", text or "")
                            for g in _GREETING_SHAPES))
    components = {
        "letter_shape": bool(letter_shape),
        "bank_identity": bool(bank_identity),
        "account_facts": bool(account_facts),
        "holder_signal": bool(holder_signal),
        "payment_marks": payment_instruction_marks(text or ""),
        "statement_marks": any(m in t for m in _STATEMENT_MARKS),
    }
    strong = all(components[k] for k in THRESHOLDS["bank_letter"]["require"])
    return {"components": components, "bank_letter_strong": strong}
