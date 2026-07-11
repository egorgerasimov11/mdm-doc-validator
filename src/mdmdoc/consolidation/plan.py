"""plan.py — Pass A: pre-write review of a consolidation write plan.

The converter already filters placeholders and refuses descriptive text in
code columns; this pass re-checks INDEPENDENTLY at write time (second lexicon,
own regexes) so a converter regression cannot silently reach the template.
Errors block the write entirely; warnings need an explicit operator confirm.
"""
from __future__ import annotations

import re

from ..rules import bankmath
from .template_io import cell_text

# --- independent junk lexicon (deliberately NOT the converter's) --------------
_JUNK_RES = (
    re.compile(r"^\((?:required|optional|if |for |see )", re.IGNORECASE),
    re.compile(r"^(?:n/?a|n\.a\.?|none|not\s+applicable|tbd|t\.b\.d\.?|\?+)$",
               re.IGNORECASE),
    re.compile(r"^(?:please\s+select|select\s+one|requires?\s+a\s+selection)",
               re.IGNORECASE),
    re.compile(r"^(?:see\s+attach(?:ed|ment)s?)$", re.IGNORECASE),
    re.compile(r"^[Ññ]$"),
)

# SAP code columns: short alphanumeric tokens only — a description leaking in
# ("Z000 - Payable Immediately") would corrupt the upload.
_CODE_COLS = {
    "ZWELS", "ZTERM", "QSREC", "KTOKK", "BU_GROUP", "AKONT", "ZUAWA",
    "FDGRV", "WAERS", "SPRAS", "LANGU", "BU_LANGU", "LAND1", "COUNTRY",
    "BANKS", "REGIO", "REGION", "BUKRS", "EKORG", "RLTYP", "TAXTYPE",
    "BKVID", "_ACTION_CODE", "TYPE",
}
_CODE_RE = re.compile(r"^[A-Za-z0-9_]{1,10}$")

# SAP field lengths (raw DB); exceeding these truncates or errors on import.
_MAX_LEN = {
    "LIFNR": 10, "PARTNER": 10, "SORTL": 10, "BU_SORT1": 20, "BU_SORT2": 20,
    "NAME1": 35, "NAME2": 35, "NAME_ORG1": 40, "NAME_ORG2": 40,
    "STRAS": 35, "STREET": 60, "ORT01": 35, "CITY1": 40, "ORT02": 35,
    "PSTLZ": 10, "POST_CODE1": 10, "REGIO": 3, "REGION": 3,
    "STCD1": 16, "STCD2": 11, "STCD3": 18, "STCD4": 18, "STCEG": 20,
    "BANKL": 15, "BANKN": 18, "IBAN": 34, "KOINH": 60, "TAXNUM": 20,
    "SMTP_ADDR": 241, "TEL_NUMBER": 30, "FAX_NUMBER": 30,
    "BUKRS": 4, "EKORG": 4, "KTOKK": 4, "ZTERM": 4, "QSREC": 2,
    "AKONT": 10, "ZUAWA": 3, "FDGRV": 10, "WAERS": 5, "EIKTO": 12,
}


def _is_junk(value: str) -> bool:
    s = value.strip()
    return any(rx.match(s) for rx in _JUNK_RES)


def _aba_findings(cells_by_row: dict) -> list[dict]:
    """US routing numbers must be 9 digits with a valid ABA 3-7-1 checksum.
    An 8-digit value whose zero-padded form passes the checksum is almost
    always a lost leading zero (Excel ate it) — suggest, never auto-fix."""
    out = []
    for (sheet, row), fields in cells_by_row.items():
        if not sheet.startswith("BUT0BK"):
            continue
        if cell_text(fields.get("BANKS", "")).upper() != "US":
            continue
        raw = cell_text(fields.get("BANKL", ""))
        if not raw:
            continue
        digits = bankmath.digits(raw)
        if len(digits) == 9 and raw == digits:
            if not bankmath.checksum_valid(digits):
                out.append(_w("ABA", sheet, "BANKL",
                              f"routing {raw!r}: ABA 3-7-1 checksum fails — "
                              "cannot be a real US routing number"))
        elif len(digits) == 8 and bankmath.checksum_valid("0" + digits):
            out.append(_w("ABA", sheet, "BANKL",
                          f"routing {raw!r} has 8 digits; a US ABA has 9. "
                          f"'0{digits}' passes the checksum — the leading zero "
                          "was probably lost. Fix the source value and "
                          "re-extract, or consolidate as-is deliberately."))
        else:
            out.append(_w("ABA", sheet, "BANKL",
                          f"routing {raw!r}: a US routing number is exactly "
                          f"9 digits (got {len(digits)})"))
    return out


def _w(code, sheet, tech, message):
    return {"level": "warning", "code": code, "sheet": sheet, "tech": tech,
            "message": message}


def _e(code, sheet, tech, message):
    return {"level": "error", "code": code, "sheet": sheet, "tech": tech,
            "message": message}


def review(planned_cells: list[dict], *, kind: str, source_id: str) -> dict:
    """-> {"errors": [...], "warnings": [...]} over a dry-run write plan."""
    errors, warnings = [], []

    cells_by_row: dict = {}
    for c in planned_cells:
        cells_by_row.setdefault((c["sheet"], c["row"]), {})[c["tech"]] = c["value"]

    seen_source_ids = set()
    for c in planned_cells:
        sheet, tech, value = c["sheet"], c["tech"], c["value"]
        if tech == "_COMMENT":
            errors.append(_e("COMMENT", sheet, tech,
                             "_COMMENT in the write plan — SAP would skip the row"))
        if _is_junk(value):
            errors.append(_e("JUNK", sheet, tech,
                             f"placeholder text {value!r} would land in "
                             f"{sheet}.{tech}"))
        if value.startswith("="):
            errors.append(_e("FORMULA", sheet, tech,
                             f"{value!r} starts with '=' — Excel would "
                             "evaluate it as a formula"))
        if tech in _CODE_COLS and not _CODE_RE.fullmatch(value):
            errors.append(_e("CODE", sheet, tech,
                             f"{value!r} is not a bare SAP code for "
                             f"{sheet}.{tech} (letters/digits/_ up to 10)"))
        limit = _MAX_LEN.get(tech)
        if limit and len(value) > limit:
            warnings.append(_w("LEN", sheet, tech,
                               f"{sheet}.{tech} value is {len(value)} chars; "
                               f"SAP field takes {limit} — will truncate on import"))
        # SOURCE_ID carries the per-vendor link and must equal this vendor's
        # SOURCE_ID everywhere. ASSIGNMENT_ID (fixed constant 000000000001) and
        # SOURCE_ADDRNUMBER (the per-vendor address number "1", not the vendor
        # link) are NOT that link, so they are excluded here.
        if tech == "SOURCE_ID":
            seen_source_ids.add(value)

    warnings.extend(_aba_findings(cells_by_row))

    # one vendor == one SOURCE_ID everywhere
    if seen_source_ids and seen_source_ids != {source_id}:
        errors.append(_e("SID", "", "",
                         f"metadata ids in plan {sorted(seen_source_ids)} != "
                         f"vendor SOURCE_ID {source_id!r}"))
    if not seen_source_ids and source_id:
        errors.append(_e("SID", "", "",
                         "no SOURCE_ID cells in plan — rows of this vendor "
                         "would not link across sheets on import"))

    # minimum viability by vendor kind
    sheets = {c["sheet"] for c in planned_cells}
    techs = {(c["sheet"], c["tech"]) for c in planned_cells}
    if kind == "form":
        if ("LFA1 - Supplier General", "NAME1") not in techs:
            errors.append(_e("MIN", "LFA1 - Supplier General", "NAME1",
                             "vendor name missing from plan"))
        if ("LFA1 - Supplier General", "LAND1") not in techs:
            errors.append(_e("MIN", "LFA1 - Supplier General", "LAND1",
                             "vendor country missing from plan"))
        if "LFB1 - Company Code (Supplier)" not in sheets:
            warnings.append(_w("MIN", "LFB1 - Company Code (Supplier)", "",
                               "no company-code rows — SAP vendor will have "
                               "no company-code view"))
    elif kind == "doc":
        short = ", ".join(sorted(s.split(" - ")[0] for s in sheets))
        warnings.append(_w("MIN", "", "",
                           f"document fragment writes only {short} — not a "
                           "full vendor record; make sure the SOURCE_ID ties "
                           "it to the intended partner before SAP import"))
    if not planned_cells:
        errors.append(_e("MIN", "", "", "empty write plan — nothing to consolidate"))

    return {"errors": errors, "warnings": warnings}
