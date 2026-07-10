#!/usr/bin/env python3
"""bulk.bank — per-row checks for BUT0BK-shaped bank-details exports.

Rule ids (catalog in docs/BULK_VALIDATION.md + the template README):
  BULK-B01  US bank key is not a 9-digit routing number            INVALID
  BULK-B02  ABA 3-7-1 mod-10 checksum failed                       INVALID
  BULK-B03  Fed routing-symbol prefix never assigned               INVALID
  BULK-B04  account: all zeros / <4 significant digits             INVALID/SUSP
  BULK-B05  IBAN checksum (mod-97) or national length failed       INVALID
  BULK-B06  IBAN country prefix != Bank Country                    SUSPICIOUS
  BULK-B07  SWIFT/BIC shape or country mismatch                    INVALID/SUSP
  BULK-B08  control key not in the country's convention            SUSPICIOUS
  BULK-B09  same account under several Business Partners           SUSPICIOUS
  BULK-B10  national bank key shape wrong for the country          INVALID
  BULK-B11  empty / masked account value                           SKIPPED
  BULK-B12  routing not listed in live public directories (web)    SUSPICIOUS

US routing arithmetic is rules/bankmath.py — the SAME functions behind the
document rules (one source of truth); IBAN length table comes from the shared
rules/banking.yaml `tables:` block.
"""
from __future__ import annotations

import re

import yaml

from .. import config
from ..fields import iban_mod97_ok, to_iso2
from ..rules import bankmath
from .model import RowVerdict

_DATA_PATH = config.RULES_DIR / "bulk_bank.yaml"
_MASKED_RE = re.compile(r"^(?:[X]{4,}|[\*•#]{2,})[\d\-]{0,6}$|^[X\*•#]{3,}$",
                        re.IGNORECASE)
_BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def load_data() -> dict:
    d = yaml.safe_load(_DATA_PATH.read_text(encoding="utf-8")) or {}
    return {"key_shapes": d.get("key_shapes") or {},
            "control_keys": d.get("control_keys") or {},
            "iban_countries": {str(c).upper() for c in d.get("iban_countries") or []}}


def _iban_lengths() -> dict:
    from ..rules.engine import load_rules
    try:
        return load_rules("bank").get("tables", {}).get("iban_length", {}) or {}
    except Exception:
        return {}


def _norm(v) -> str:
    return re.sub(r"[\s\-.]", "", str(v or "")).upper()


def check_rows(rows: list[dict], web: bool = False, notes: list | None = None,
               data: dict | None = None, progress=None) -> list[RowVerdict]:
    say = progress or (lambda s: None)
    d = data or load_data()
    iban_len = _iban_lengths()
    notes = notes if notes is not None else []
    out: list[RowVerdict] = []

    # duplicate map: country|key|account (zero-padding stripped) and IBAN
    dup: dict[str, set] = {}
    for row in rows:
        acct = _norm(row.get("bank_account")).lstrip("0")
        key = _norm(row.get("bank_key"))
        iban = _norm(row.get("iban"))
        partner = str(row.get("partner") or "")
        if acct:
            dup.setdefault(f"{key}|{acct}", set()).add(partner)
        if iban:
            dup.setdefault(f"iban|{iban}", set()).add(partner)

    for i, row in enumerate(rows, start=1):
        if i % 500 == 0:
            say(f"bank rows {i}/{len(rows)}")
        rv = RowVerdict(row_no=i, key=str(row.get("partner") or ""))
        cc = to_iso2(str(row.get("bank_country") or "")) \
            or _norm(row.get("bank_country"))[:2]
        bank_key = str(row.get("bank_key") or "").strip()
        account = str(row.get("bank_account") or "").strip()
        iban = _norm(row.get("iban"))
        swift = _norm(row.get("swift_bic"))

        # --- account ------------------------------------------------------------
        if not account and not iban:
            rv.skip("BULK-B11", "no account number and no IBAN in the row")
        elif account and _MASKED_RE.match(_norm(account)):
            rv.skip("BULK-B11", "account value is masked in the export")
        elif account:
            sig = bankmath.significant_digits(account)
            if sig == 0:
                rv.hit("INVALID", "BULK-B04", "account is all zeros")
            elif sig < 4:
                rv.hit("SUSPICIOUS", "BULK-B04",
                       f"only {sig} significant digit(s) after removing zero "
                       "padding — real accounts have at least 4")

        # --- bank key -----------------------------------------------------------
        if bank_key:
            if cc == "US":
                digits = bankmath.digits(bank_key)
                if not re.fullmatch(r"\d{9}", bank_key.strip()):
                    if bankmath.looks_like_bic(bank_key):
                        rv.hit("INVALID", "BULK-B01",
                               "bank key holds a SWIFT/BIC, not a routing number")
                    elif len(digits) == 9 and bankmath.checksum_valid(digits):
                        rv.hit("SUSPICIOUS", "BULK-B01",
                               "routing has stray punctuation — cleans to a "
                               "checksum-valid 9-digit number")
                    else:
                        rv.hit("INVALID", "BULK-B01",
                               f"{len(bank_key)} chars — a US routing number is "
                               "exactly 9 digits")
                elif not bankmath.checksum_valid(bank_key):
                    s = bankmath.checksum_sum(bank_key)
                    rv.hit("INVALID", "BULK-B02",
                           f"ABA 3-7-1 weighted sum {s} % 10 = {s % 10} ≠ 0 — "
                           "mathematically cannot be a real routing number")
                elif not bankmath.prefix_valid(bank_key):
                    rv.hit("INVALID", "BULK-B03",
                           f"routing prefix {bank_key[:2]} is never assigned by "
                           "the Fed (valid: 00, 01-12, 21-32, 61-72, 80)")
            else:
                shape = d["key_shapes"].get(cc)
                if shape and not re.fullmatch(str(shape["regex"]), _norm(bank_key)):
                    rv.hit("INVALID", "BULK-B10",
                           f"bank key does not look like a {cc} "
                           f"{shape.get('name', 'bank key')} ({shape['regex']})")

        # --- IBAN ---------------------------------------------------------------
        if iban:
            if not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]+$", iban):
                rv.hit("INVALID", "BULK-B05", "IBAN does not start CCkk…")
            else:
                exp = iban_len.get(iban[:2])
                if exp and len(iban) != int(exp):
                    rv.hit("INVALID", "BULK-B05",
                           f"IBAN length {len(iban)} ≠ {exp} for {iban[:2]}")
                elif not iban_mod97_ok(iban):
                    rv.hit("INVALID", "BULK-B05",
                           "IBAN checksum failed (ISO 13616 mod-97)")
                if cc and iban[:2] != cc:
                    rv.hit("SUSPICIOUS", "BULK-B06",
                           f"IBAN prefix {iban[:2]} ≠ bank country {cc}")

        # --- SWIFT --------------------------------------------------------------
        if swift:
            if not _BIC_RE.match(swift):
                rv.hit("INVALID", "BULK-B07",
                       "SWIFT/BIC shape invalid (6 letters + 2/5 alphanumerics)")
            elif cc and swift[4:6].isalpha() and swift[4:6] != cc:
                rv.hit("SUSPICIOUS", "BULK-B07",
                       f"SWIFT country {swift[4:6]} ≠ bank country {cc}")

        # --- control key ----------------------------------------------------------
        ck = str(row.get("control_key") or "").strip()
        ck_spec = d["control_keys"].get(cc)
        if ck and ck_spec and ck not in ck_spec.get("allowed", []):
            rv.hit("SUSPICIOUS", "BULK-B08",
                   f"control key '{ck}' outside the {cc} convention "
                   f"({ck_spec.get('note', '')})")

        # --- duplicates -----------------------------------------------------------
        acct_n = _norm(account).lstrip("0")
        for dkey in ((f"{_norm(bank_key)}|{acct_n}" if acct_n else ""),
                     (f"iban|{iban}" if iban else "")):
            partners = dup.get(dkey) or set()
            if len(partners) > 1:
                others = sorted(p for p in partners if p != rv.key)[:6]
                rv.hit("SUSPICIOUS", "BULK-B09",
                       f"same account under {len(partners)} partners "
                       f"(e.g. {', '.join(others)})")
                break
        out.append(rv)

    # --- optional web existence layer (unique checksum-valid US routings) -------
    if web:
        from . import webcheck
        us_rows = [(rv, _norm(row.get("bank_key")))
                   for rv, row in zip(out, rows)
                   if to_iso2(str(row.get("bank_country") or "")) == "US"
                   and re.fullmatch(r"\d{9}", _norm(row.get("bank_key")))
                   and bankmath.checksum_valid(_norm(row.get("bank_key")))]
        uniq = [r for _, r in us_rows]
        say(f"web layer: {len(set(uniq))} unique checksum-valid US routings")
        results = webcheck.routing_existence(uniq, progress=say)
        unchecked = sum(1 for e in results.values()
                        if e["status"] in ("unavailable", "unchecked"))
        if unchecked:
            notes.append(f"web layer: {unchecked} routing(s) could not be "
                         "checked (directories unreachable) — deterministic "
                         "verdicts unaffected")
        for rv, aba in us_rows:
            e = results.get(aba) or {}
            if e.get("status") == "not_found":
                rv.hit("SUSPICIOUS", "BULK-B12", e.get("note", ""))
            elif e.get("status") == "found":
                rv.reasons.append(e.get("note", ""))
    return out
