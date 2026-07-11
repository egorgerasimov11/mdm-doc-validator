"""required_fields.py — fill SAP-mandatory KEY fields the converter leaves blank.

SAP's consolidation import rejects rows whose mandatory key fields are initial,
e.g. "Initial value for field ADR2-DATE_FROM not allowed". The KEY fields are
the UNDERLINED headers in the template (BPTemplate.key_fields()). For every
emitted row, any underlined key field that is empty and has an entry in
DEFAULTS is filled:

  DATE_FROM / *_FROM (validity-start date keys) -> today (YYYYMMDD, ABAP DATS)
  CONSNUMBER (address sub-entry sequence)        -> "001"

Other underlined keys (NATION, DFVAL, …) intentionally stay blank: SAP's own
SUPPLIER.xlsx example leaves them empty and imports fine, so a guessed value
would deviate from a proven-good record. DEFAULTS is extensible if SAP later
rejects another specific field.
"""
from __future__ import annotations

from .template_io import cell_text

CONSNUMBER_DEFAULT = "001"

# SAP needs ASSIGNMENT_ID as a numeric assignment key; the converter copies the
# SOURCE_ID into it (e.g. NEW_20260711_01) which SAP rejects
# ("LFA1-ASSIGNMENT_ID did not accept source value …"). A vendor's assignment
# number is the fixed constant below.
ASSIGNMENT_ID_VENDOR = "000000000001"

# SAP business-partner category (BUT000-TYPE): 1=person, 2=organization, 3=group.
# We always create vendors as organizations; SAP rejects an empty/other value
# ("Business partner category does not exist; use 1, 2 or 3").
BP_CATEGORY_ORG = "2"

# Vendor/address language = the SAP 1-char language key (SPRAS domain, CHAR1).
# SAP's own examples use "D" (German); English is "E". A 2-char ISO ("EN") gets
# compressed to 1 char and warns ("assumed value"/"data loss EN instead of EN").
VENDOR_LANGUAGE = "E"
_LANGUAGE_SET = {
    "LFA1 - Supplier General": ("SPRAS",),      # vendor language
    "ADRC - Address": ("LANGU",),               # address language (mandatory)
}
# The BUT000 BP-level language is a PERSON attribute — SAP's examples leave it
# blank on organizations, and setting it warns "may be maintained only for
# persons" (+ data loss). Blank it (override the converter's form language).
_LANGUAGE_BLANK = {
    "BUT000 - General": ("BU_LANGU", "LANGU_CORR"),
}

# LFB1 "Check Double Invoice" — mandatory in the operator's field-status config
# ("Enter a value for field REPRF"); "X" = enable the duplicate-invoice check.
REPRF_CHECK_DOUBLE_INVOICE = "X"


def apply_constants(rows_by_sheet: dict, columns=None) -> dict:
    """Override converter-set values that SAP requires as fixed constants.
    Mutates and returns rows_by_sheet. Runs on every build path (incl. the
    Pass-D re-derivation) so the values are deterministic and verification
    matches. `columns` (a BPTemplate) guards fields the converter doesn't emit
    (e.g. LANGU_CORR) so a row never gains a column the writer would reject."""
    def has(sheet, field):
        return columns is None or columns.column_for(sheet, field) is not None

    for rows in rows_by_sheet.values():
        for row in rows:
            if "ASSIGNMENT_ID" in row:
                row["ASSIGNMENT_ID"] = ASSIGNMENT_ID_VENDOR
    # BP category on BUT000 only — NOT BUT0ID.TYPE (identifier type) or
    # DFKKBPTAXNUM.TAXTYPE, which are different fields.
    for row in rows_by_sheet.get("BUT000 - General", []):
        row["TYPE"] = BP_CATEGORY_ORG
    # Address + vendor language = the SAP 1-char key. Set on all ADRC rows → the
    # intl version row inherits it via the clone.
    for sheet, fields in _LANGUAGE_SET.items():
        for row in rows_by_sheet.get(sheet, []):
            for field in fields:
                if has(sheet, field):
                    row[field] = VENDOR_LANGUAGE
    # BUT000 BP-level language stays blank on organizations (person-only in SAP).
    for sheet, fields in _LANGUAGE_BLANK.items():
        for row in rows_by_sheet.get(sheet, []):
            for field in fields:
                if field in row:
                    row[field] = ""
    # REPRF (Check Double Invoice) on every LFB1 row.
    lfb1 = "LFB1 - Company Code (Supplier)"
    if has(lfb1, "REPRF"):
        for row in rows_by_sheet.get(lfb1, []):
            row["REPRF"] = REPRF_CHECK_DOUBLE_INVOICE
    return rows_by_sheet


def _defaults(today: str) -> dict:
    return {
        "DATE_FROM": today,
        "VALID_FROM": today,
        "CONSNUMBER": CONSNUMBER_DEFAULT,
    }


def _default_for(tech: str, defaults: dict):
    if tech in defaults:
        return defaults[tech]
    # any other *_FROM date key -> the validity-start date
    if tech.endswith("_FROM"):
        return defaults["DATE_FROM"]
    return None


def fill(rows_by_sheet: dict, key_fields: dict, today: str) -> dict:
    """Fill empty underlined key fields with their default. Mutates and returns
    rows_by_sheet. `today` is YYYYMMDD (captured once per consolidation so the
    round-trip verification re-derives the same value)."""
    defaults = _defaults(today)
    for sheet, rows in rows_by_sheet.items():
        keys = key_fields.get(sheet) or set()
        if not keys:
            continue
        for row in rows:
            for tech in keys:
                if cell_text(row.get(tech)):
                    continue
                val = _default_for(tech, defaults)
                if val is not None:
                    row[tech] = val
    return rows_by_sheet
