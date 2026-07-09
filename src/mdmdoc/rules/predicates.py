#!/usr/bin/env python3
"""
predicates.py — named python checks callable from rules/*.yaml via `check:`.

Contract: predicate(value, extraction_fields, args, tables) -> (fired, detail)
`fired=True` means the RULE FIRES (i.e. a problem was found). Predicates never
raise — the engine wraps them and emits engine_error findings on exceptions.
(SWIFT/IBAN logic mirrors form-validator rules_engine.py chk_swift/chk_iban.)
"""
from __future__ import annotations

import re
from datetime import datetime

from ..fields import _norm_id, looks_like_business, norm_classification, to_iso2


def swift_valid(value, flds, args, tables):
    sw = _norm_id(value)
    if not sw:
        return False, ""
    lengths = args.get("lengths", [8, 11])
    if len(sw) not in lengths:
        return True, f"length {len(sw)}, must be one of {lengths}"
    if not re.match(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$", sw):
        return True, "not a valid BIC shape (6 letters + 2/5 alphanumerics)"
    bank_country = to_iso2(str(flds.get("bank_country") or ""))
    cc = sw[4:6]
    if bank_country and cc.isalpha() and cc != bank_country:
        return True, f"SWIFT country ({cc}) differs from bank country ({bank_country})"
    return False, ""


def iban_valid(value, flds, args, tables):
    iban = _norm_id(value)
    if not iban:
        return False, ""
    if not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]+$", iban):
        # A purely numeric value is a plain account number — the US and other
        # non-IBAN countries have no IBAN, so a domestic/international account
        # number in this field is a NORMAL format, not a malformed IBAN.
        # (Stage B already relocates it to account_number; this is defence.)
        if iban.isdigit():
            return False, ""
        return True, "non-standard shape (may be a plain account number in the IBAN field)"
    cc = iban[:2]
    table = tables.get(args.get("table", "iban_length"), {})
    exp = table.get(cc)
    if exp and len(iban) != int(exp):
        return True, f"length {len(iban)} differs from expected {exp} for {cc}"
    bank_country = to_iso2(str(flds.get("bank_country") or ""))
    if bank_country and cc != bank_country:
        return True, f"IBAN prefix ({cc}) differs from bank country ({bank_country})"
    from ..fields import iban_mod97_ok
    if not iban_mod97_ok(iban):
        return True, "checksum failed (ISO 13616 mod-97) — an OCR misread or a typo"
    return False, ""


def ein_shape(value, flds, args, tables):
    v = str(value or "").strip()
    if not v:
        return False, ""
    d = re.sub(r"\D", "", v)
    if len(d) != int(args.get("digits", 9)):
        return True, f"{len(d)} digits, must be {args.get('digits', 9)}"
    return False, ""


def tin_type_vs_classification(value, flds, args, tables):
    """Individual/Sole proprietor aligns with SSN; business classifications with EIN."""
    cls = norm_classification(str(flds.get("line3_classification") or ""))
    tt = str(flds.get("tin_type") or "").upper()
    if not cls or not tt:
        return False, ""
    if cls == "individual_sole_prop" and tt == "EIN" and not str(flds.get("line2_business_name") or "").strip():
        return True, "Individual/Sole proprietor classification with EIN and no business name"
    if cls in ("llc", "corporation", "partnership", "trust_estate") and tt == "SSN":
        return True, f"{cls} classification with an SSN"
    return False, ""


def individual_with_business_name_and_ein(value, flds, args, tables):
    """The DR-20260624-131532 internal-inconsistency triple."""
    cls = norm_classification(str(flds.get("line3_classification") or ""))
    tt = str(flds.get("tin_type") or "").upper()
    biz = (looks_like_business(str(flds.get("line1_name") or ""))
           or looks_like_business(str(flds.get("line2_business_name") or "")))
    if cls == "individual_sole_prop" and tt == "EIN" and biz:
        return True, "Individual classification + business/LLC name + EIN together"
    return False, ""


_PERSON_NAME_RE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z]+){1,2}$")


def _looks_like_person(name: str) -> bool:
    """Strict person-name shape (First [M.] Last). 'Culligan of Denver' is a
    trade name, NOT a person — a bare 'no business suffix' test was too eager."""
    n = (name or "").strip()
    if not n or any(ch.isdigit() for ch in n):
        return False
    low = f" {n.lower()} "
    if any(w in low for w in (" of ", " the ", " and ", " & ", " for ")):
        return False
    from .. import fields as _f
    if _f.looks_like_business(n):
        return False
    return bool(_PERSON_NAME_RE.match(n))


def line_swap_suspect(value, flds, args, tables):
    """Line 1 = legal taxpayer, Line 2 = business/DBA is the NORMAL W-9
    structure — never flag it. Only flag an empty Line 1 with a filled Line 2,
    or an Individual whose Line 1 is a business while Line 2 is a person name."""
    l1 = str(flds.get("line1_name") or "").strip()
    l2 = str(flds.get("line2_business_name") or "").strip()
    if not l1 and l2:
        return True, "Line 1 empty while Line 2 present — lines likely swapped/collapsed"
    cls = norm_classification(str(flds.get("line3_classification") or ""))
    if cls == "individual_sole_prop" and looks_like_business(l1) and _looks_like_person(l2):
        return True, "Line 1 looks like a business while Line 2 looks like a person (Individual classification)"
    return False, ""


_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y", "%B %d, %Y",
                 "%d %B %Y", "%b %d, %Y", "%m/%d/%y", "%d.%m.%y",
                 # day-first abbreviated + comma variants (audit C13): "15 Jan 2023"
                 # used to fall through to the year-only fallback (-> July 1) while
                 # the ABAP twin parsed it — a live cross-implementation divergence
                 "%d %b %Y", "%d %b, %Y", "%d %B, %Y", "%B %d %Y", "%b %d %Y")
_MONTHS = {  # minimal ES/DE month mapping for bank letters
    "enero": "January", "febrero": "February", "marzo": "March", "abril": "April",
    "mayo": "May", "junio": "June", "julio": "July", "agosto": "August",
    "septiembre": "September", "octubre": "October", "noviembre": "November",
    "diciembre": "December", "januar": "January", "februar": "February", "märz": "March",
    "mai": "May", "juni": "June", "juli": "July", "oktober": "October", "dezember": "December",
}


def parse_date(s: str):
    v = str(s or "").strip()
    if not v:
        return None
    low = v.lower()
    for es, en in _MONTHS.items():
        # whole-word only: 'januar' must not rewrite English 'january' -> 'Januaryy'
        if re.search(rf"\b{es}\b", low):
            v = re.sub(rf"(?i)\b{es}\b", en, v)
            v = re.sub(r"\bde\b", " ", v, flags=re.I)
            v = re.sub(r"\s+", " ", v).strip()
            break
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    m = re.search(r"(20\d{2})", v)
    if m:  # year-only fallback: mid-year
        try:
            return datetime(int(m.group(1)), 7, 1)
        except ValueError:
            return None
    return None


def _now() -> datetime:
    """Injectable clock: MDMDOC_NOW (ISO date/datetime) pins 'today' for tests
    and golden runs; invalid or unset -> real now(). Predicates never raise."""
    import os
    v = os.environ.get("MDMDOC_NOW", "").strip()
    if v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
    return datetime.now()


def date_older_than(value, flds, args, tables):
    dt = parse_date(str(value or ""))
    if dt is None:
        return False, ""
    years = int(args.get("years", 2))
    age_days = (_now() - dt).days
    if age_days > years * 365:
        return True, f"document date {dt.date()} is ~{age_days // 365} years old"
    return False, ""


_EV_POSITIVE = ("computer generated", "computer-generated", "system generated",
                "system-generated", "electronically", "electronic confirmation",
                "requires no signature", "no signature required", "verification code",
                "typed officer", "officer block", "officer name", "contact block",
                "digital signature", "digitally signed", "qr code", "printed signature",
                "signature-like", "docusign", "e-signature", "electronic signature",
                "title block", "name and title")


def _positive_evidence(ev: str) -> bool:
    """Evidence compensates for a missing wet signature only when it NAMES a
    compensating artifact (officer block, computer-generated notice, digital
    verification). 'No signature or stamp is present' is a finding of ABSENCE,
    not evidence — it must not silence the unsigned warning."""
    t = (ev or "").strip().lower()
    return bool(t) and any(m in t for m in _EV_POSITIVE)


def unsigned_no_evidence(value, flds, args, tables):
    """Unsigned AND nothing standing in for a signature — a bare letter."""
    signed = flds.get("signed")
    signed = signed if isinstance(signed, bool) else str(signed).lower() in ("true", "yes")
    if signed:
        return False, ""
    ev = str(flds.get("signature_evidence") or "").strip()
    if _positive_evidence(ev):
        return False, ""
    return True, (f"no signature, and the noted '{ev}' is a statement of absence, "
                  "not a compensating artifact") if ev else \
                 "no signature, stamp or officer block visible"


def unsigned_typed_block(value, flds, args, tables):
    """No wet signature, but a compensating artifact IS present (typed officer
    block / computer-generated notice) — common for bank e-letters; a note,
    not a blocker."""
    signed = flds.get("signed")
    signed = signed if isinstance(signed, bool) else str(signed).lower() in ("true", "yes")
    ev = str(flds.get("signature_evidence") or "").strip()
    if not signed and _positive_evidence(ev):
        return True, ev
    return False, ""


def field_empty(value, flds, args, tables):
    """Generic 'the field is not shown in the document' predicate."""
    if str(value or "").strip():
        return False, ""
    return True, "not shown in the document"


def no_bank_ids(value, flds, args, tables):
    for k in ("iban", "account_number", "routing_aba"):
        if str(flds.get(k) or "").strip():
            return False, ""
    return True, "no IBAN, account number or routing/ABA found"


REGISTRY = {
    "unsigned_no_evidence": unsigned_no_evidence,
    "unsigned_typed_block": unsigned_typed_block,
    "field_empty": field_empty,
    "no_bank_ids": no_bank_ids,
    "swift_valid": swift_valid,
    "iban_valid": iban_valid,
    "ein_shape": ein_shape,
    "tin_type_vs_classification": tin_type_vs_classification,
    "individual_with_business_name_and_ein": individual_with_business_name_and_ein,
    "line_swap_suspect": line_swap_suspect,
    "date_older_than": date_older_than,
}
