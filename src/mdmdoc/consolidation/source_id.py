"""SOURCE_ID for consolidation rows.

SAP's consolidation import ties one vendor's rows across all sheets through
SOURCE_ID (+ source system). For EXISTING vendors the form's Vendor Number is
the SOURCE_ID (converter behavior, unchanged). For NEW vendors (internal
numbering — PARTNER/LIFNR stay blank) the SOURCE_ID must still be present and
identical on every row, so we generate one and let the operator edit it
before consolidating.
"""
from __future__ import annotations

import re
from datetime import datetime

_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def generate(existing: set[str], today: str | None = None) -> str:
    """NEW_<YYYYMMDD>_<NN> — first ordinal free against `existing`."""
    stamp = today or datetime.now().strftime("%Y%m%d")
    for nn in range(1, 100):
        sid = f"NEW_{stamp}_{nn:02d}"
        if sid not in existing:
            return sid
    raise ValueError(f"no free NEW_{stamp}_NN source id (99 used?)")


def problems(sid: str, existing: set[str]) -> list[str]:
    """Validation messages; empty list == usable."""
    out = []
    sid = (sid or "").strip()
    if not sid:
        out.append("SOURCE_ID is empty")
        return out
    if not _SID_RE.fullmatch(sid):
        out.append(
            "SOURCE_ID must be 1-20 chars of letters/digits/_/- "
            f"(got {sid!r})"
        )
    if sid in existing:
        out.append(
            f"SOURCE_ID {sid!r} already exists in the template — appending it "
            "again would merge two vendors into one record during SAP import"
        )
    return out
