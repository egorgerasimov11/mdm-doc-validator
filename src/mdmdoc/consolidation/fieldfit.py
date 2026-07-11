"""fieldfit.py — fit built row values into SAP field lengths so the import
doesn't warn "Data loss in field …".

SAP truncates every value longer than its field and warns. Rather than let SAP
silently truncate, fit the values here first:

- long NAME / STREET SPILL into their empty continuation fields (NAME2..4,
  NAME_ORG2..4, STR_SUPPL1..3) at word boundaries — the data is PRESERVED;
- search keys and legacy single fields (SORTL, BU_SORT1/2, LFA1-STRAS, …) are
  TRUNCATED to the field length (SAP truncates them anyway → same stored value,
  no warning).

(SOURCE_ADDRNUMBER is handled in `address.py`, not here — it must be a valid
non-initial address number "1", not blanked.)

Bank / tax / code / date fields are deliberately absent from every map here, so
a bank account or tax number can NEVER be silently truncated. Pure function of
the row values → deterministic, so the Pass-D re-derivation stays identical.
"""
from __future__ import annotations

from .template_io import cell_text

# Name / street families that SPILL overflow into empty continuation slots.
# The vendor name is a cross-sheet invariant (verify compares it across
# LFA1/BUT000/ADRC), so all three name families use the SAME first-field limit
# (35, the smallest — LFA1.NAME1) → the name splits IDENTICALLY on every sheet.
# ADRC.NAME1/NAME_ORG1 physically allow 40, but 35 fits them too and keeps the
# split consistent (and avoids plan.py's flat NAME1=35 length check false-firing).
_SPILL_GROUPS = [
    ("LFA1 - Supplier General",
     [("NAME1", 35), ("NAME2", 35), ("NAME3", 35), ("NAME4", 35)]),
    ("BUT000 - General",
     [("NAME_ORG1", 35), ("NAME_ORG2", 40), ("NAME_ORG3", 40), ("NAME_ORG4", 40)]),
    ("ADRC - Address",
     [("NAME1", 35), ("NAME2", 40), ("NAME3", 40), ("NAME4", 40)]),
    # STREET physically holds 60, but SAP restricts chars 36-60 ("last 25
    # characters … restricted"), so fit to 35 and spill into STR_SUPPL.
    ("ADRC - Address",
     [("STREET", 35), ("STR_SUPPL1", 40), ("STR_SUPPL2", 40), ("STR_SUPPL3", 40)]),
]

# Truncate-only text fields (no continuation): search keys, legacy mirrors, city.
_TRUNC = {
    "LFA1 - Supplier General": {"STRAS": 35, "SORTL": 10, "ORT01": 35, "ORT02": 35},
    "BUT000 - General": {"BU_SORT1": 20, "BU_SORT2": 20},
    "ADRC - Address": {"SORT2": 20, "CITY1": 40, "CITY2": 40},
}

def _split_at_word(text: str, limit: int) -> tuple[str, str]:
    """Split into (head ≤ limit, tail). Prefer the last space at or before the
    limit; hard-split only when there is no space to break on."""
    if len(text) <= limit:
        return text, ""
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(), text[cut:].lstrip()


def _spill(row: dict, group: list[tuple[str, int]]) -> None:
    """Cascade the first field's overflow into EMPTY continuation slots, then
    cap every field in the group at its own limit."""
    first, first_lim = group[0]
    text = cell_text(row.get(first))
    if len(text) > first_lim:
        head, tail = _split_at_word(text, first_lim)
        row[first] = head
        for field, lim in group[1:]:
            if not tail:
                break
            if cell_text(row.get(field)):      # occupied → keep it, stop spilling
                break
            head, tail = _split_at_word(tail, lim)
            row[field] = head
        # any residual `tail` is dropped — same as SAP truncating (no slot left)
    # safety: an over-long form-provided continuation is capped too
    for field, lim in group:
        v = cell_text(row.get(field))
        if len(v) > lim:
            row[field] = v[:lim].rstrip()


def fit_sap_fields(rows_by_sheet: dict, columns=None) -> dict:
    """Fit text values into SAP field lengths. Mutates and returns rows_by_sheet.
    `columns` (a BPTemplate) restricts spill to continuation columns the template
    actually has, so a row never gains a column the writer would reject."""
    def has(sheet, field):
        return columns is None or columns.column_for(sheet, field) is not None

    # 1. spill long names / streets into empty continuation fields.
    # A sheet present in rows_by_sheet exists in the template, so column_for is
    # safe; skip absent sheets before touching `columns`.
    for sheet, group in _SPILL_GROUPS:
        rows = rows_by_sheet.get(sheet)
        if not rows:
            continue
        existing = [(f, lim) for f, lim in group if has(sheet, f)]
        if not existing:
            continue
        for row in rows:
            _spill(row, existing)

    # 2. truncate remaining over-length search / legacy text fields
    for sheet, limits in _TRUNC.items():
        for row in rows_by_sheet.get(sheet, []):
            for field, lim in limits.items():
                v = cell_text(row.get(field))
                if v and len(v) > lim:
                    row[field] = v[:lim].rstrip()
    return rows_by_sheet
