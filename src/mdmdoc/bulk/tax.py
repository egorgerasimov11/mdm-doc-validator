#!/usr/bin/env python3
"""bulk.tax — per-row checks for BP tax-number exports (case 'tax').

Rule ids (cited in every reason; the full catalog lives in
docs/BULK_VALIDATION.md and the template's README sheet):
  BULK-T01  unknown tax category (not in rules/bulk_tax.yaml)      SUSPICIOUS
  BULK-T02  value does not match the category's format             INVALID
  BULK-T03  value is ANOTHER country's number (signature match)    INVALID
  BULK-T04  checksum failed for the category's algorithm           INVALID
  BULK-T05  value is masked in the export (XXXX…)                  SKIPPED
  BULK-T06  same number under several Business Partners            SUSPICIOUS
  BULK-T07  empty value                                            SKIPPED
  BULK-T08  Country column disagrees with the category's country   SUSPICIOUS
"""
from __future__ import annotations

import re

import yaml

from .. import config
from ..privacy import mask
from . import taxmath
from .model import RowVerdict

_CATALOG_PATH = config.RULES_DIR / "bulk_tax.yaml"
# fully masked (XXXXXXX) or export-masked with a visible tail (*******9999,
# **6481). X-runs need >=4 to avoid Mexican generic RFCs (XAXX…, run of 2);
# */•/# are never legitimate tax-number characters — 2+ anywhere means masked.
_MASKED_RE = re.compile(
    r"^(?:[X]{4,}|[\*•#]{2,})[\d\-]{0,6}$|^[X\*•#]{3,}$", re.IGNORECASE)


def load_catalog() -> dict:
    data = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    return {"categories": data.get("categories") or {},
            "signatures": data.get("signatures") or {}}


def _norm(v: str) -> str:
    return re.sub(r"\s", "", str(v or "").strip()).upper()


def _effective_value(row: dict) -> str:
    return str(row.get("tax_number_long") or "").strip() \
        or str(row.get("tax_number") or "").strip()


def _strip_prefix(value: str, country: str) -> str:
    """Drop the national prefix when the holder typed it ('DE137…' in DE0)."""
    v = _norm(value)
    for pfx in (country + "U", "CHE", country):   # ATU / CHE / plain ISO2
        if pfx and v.startswith(pfx) and len(v) > len(pfx):
            rest = v[len(pfx):]
            if any(ch.isdigit() for ch in rest):
                return rest
    return v


def _foreign_signature(value: str, own_country: str, signatures: dict):
    """-> (iso2, name) when the value carries ANOTHER country's national
    prefix and matches that country's signature; else None."""
    v = _norm(value)
    for key, sig in signatures.items():
        cc = key[:2]
        if cc == own_country:
            continue
        pfx = str(sig.get("prefix") or key)
        if not v.startswith(pfx):
            continue
        body = v[len(pfx):]
        if not body or not re.fullmatch(str(sig.get("body") or ""), body):
            continue
        fn = taxmath.REGISTRY.get(str(sig.get("checksum") or ""))
        if fn is not None and not fn(body)[0]:
            continue                      # prefix matched but math says no
        return cc, str(sig.get("name") or f"{cc} number")
    return None


def _us_any_structure(value: str):
    """US doctrine (Egor, mirrored in tin_bulk.py /ui/tax): the SAP category
    digit (US0..US4) carries NO authoritative SSN/EIN mapping — judge the
    NUMBER's own structure. Valid if it passes ANY of SSN / ITIN / EIN;
    else INVALID with the most specific detail."""
    d = re.sub(r"\D", "", value or "")
    if len(d) != 9:
        return False, f"{len(d)} digits — a US TIN has exactly 9"
    if d == d[0] * 9 or d in ("123456789", "987654321"):
        return False, f"placeholder pattern {d[:2]}… — a known fake TIN"
    ok_ssn, why_ssn = taxmath.us_ssn(d)
    if ok_ssn:
        return True, ""
    ok_itin, _ = taxmath.us_itin(d)
    if ok_itin:
        return True, ""
    ok_ein, why_ein = taxmath.us_ein(d)
    if ok_ein:
        return True, ""
    return False, f"fails every US structure (SSN: {why_ssn}; EIN: {why_ein})"


def check_rows(rows: list[dict], catalog: dict | None = None,
               progress=None) -> list[RowVerdict]:
    cat = catalog or load_catalog()
    categories, signatures = cat["categories"], cat["signatures"]
    out: list[RowVerdict] = []

    # duplicate map over NORMALIZED unmasked values
    by_value: dict[str, set] = {}
    for row in rows:
        v = _norm(_effective_value(row))
        if v and not _MASKED_RE.match(v):
            by_value.setdefault(v, set()).add(str(row.get("partner") or ""))

    for i, row in enumerate(rows, start=1):
        if progress and i % 500 == 0:
            progress(f"tax rows {i}/{len(rows)}")
        rv = RowVerdict(row_no=i, key=str(row.get("partner") or ""))
        raw_value = _effective_value(row)
        category = _norm(row.get("tax_category"))

        if not raw_value:
            rv.skip("BULK-T07", "empty tax number")
            out.append(rv)
            continue
        if _MASKED_RE.match(_norm(raw_value)):
            rv.skip("BULK-T05", "value is masked in the export — cannot be judged")
            out.append(rv)
            continue

        spec = categories.get(category)
        cc = str(spec.get("country") if spec else category[:2] or "").upper()

        # T03 first: a conclusive foreign signature beats a format complaint
        foreign = _foreign_signature(raw_value, cc, signatures)
        if foreign:
            rv.hit("INVALID", "BULK-T03",
                   f"value is a {foreign[1]} ({foreign[0]}) but the category "
                   f"{category or '?'} belongs to {cc or '?'} — wrong country")

        if spec is None:
            rv.hit("SUSPICIOUS", "BULK-T01",
                   f"category '{category or '(empty)'}' is not in the catalog "
                   f"(rules/bulk_tax.yaml) — judged by shape only")
        elif cc == "US":
            # category-independent structural judgment for every US* category
            if not foreign:
                ok, detail = _us_any_structure(raw_value)
                if not ok:
                    rv.hit("INVALID", "BULK-T02",
                           f"'{mask('tin', raw_value)}': {detail}")
        else:
            body = _strip_prefix(raw_value, cc)
            rx = str(spec.get("regex") or "")
            if rx and not (re.fullmatch(rx, _norm(raw_value))
                           or re.fullmatch(rx, body)):
                if not foreign:
                    # reasons carry the MASKED value only — the full value is
                    # visible in its own column of the result workbook; these
                    # strings also land in the leak-gated runs/ report
                    rv.hit("INVALID", "BULK-T02",
                           f"'{mask('tin', raw_value)}' does not match the "
                           f"{spec.get('name')} format ({rx})")
            else:
                fn = taxmath.REGISTRY.get(str(spec.get("checksum") or ""))
                if fn is not None:
                    ok, detail = fn(body)
                    if not ok:
                        rv.hit("INVALID", "BULK-T04",
                               f"{spec.get('name')}: {detail}")
                    elif detail:      # soft algorithm note (e.g. NL sole trader)
                        rv.hit("SUSPICIOUS", "BULK-T04", f"{spec.get('name')}: {detail}")

        # T08: explicit country column disagrees with the category's country
        col_cc = _norm(row.get("country"))[:2]
        if col_cc and cc and col_cc != cc:
            rv.hit("SUSPICIOUS", "BULK-T08",
                   f"Country column says {col_cc}, category {category} is {cc}")

        # T06: duplicates across partners
        v = _norm(raw_value)
        partners = by_value.get(v) or set()
        if len(partners) > 1:
            others = sorted(p for p in partners if p != rv.key)[:6]
            rv.hit("SUSPICIOUS", "BULK-T06",
                   f"same number appears under {len(partners)} partners "
                   f"(e.g. {', '.join(others)})")
        out.append(rv)
    return out
