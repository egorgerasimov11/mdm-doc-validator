#!/usr/bin/env python3
"""
template_form.py — parse an SAP MDM REQUEST WORKBOOK (the vendor-request
template the requestor fills) into a banking-fields dict, so the pipeline can
compare a support DOCUMENT against what the requestor typed into the form
(D8: 'compare with template', the missing half of compare.py's promise).

The Americas MacroEnabled MDM form puts labels in one column and values in the
adjacent cell(s) to the right ('2. Vendor Details' column D -> E), but other
regional workbooks move rows and columns — so the parser scans EVERY sheet for
known label aliases and takes the first non-empty cell up to 3 columns right.
No cell coordinates are pinned.

Privacy: template values are SECRETS like SAP values (register_secrets);
Tax Number 1 is a TIN — masked under every policy.
"""
from __future__ import annotations

import re
from pathlib import Path

# canon key -> label aliases (lowercased, punctuation collapsed by _lkey)
LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "bank_name": ("bank name",),
    "bank_country": ("bank country", "bank country region", "country of bank"),
    "bank_key": ("bank key aba routing code", "bank key", "aba routing code",
                 "routing number", "aba routing number", "sort code",
                 "bank key routing code"),
    "bank_account": ("bank account number", "account number", "account no",
                     "bank account"),
    "iban": ("iban number", "iban"),
    "swift_bic": ("bic swift code", "swift code", "bic code", "swift bic",
                  "bic swift"),
    "account_holder": ("account holder name", "account holder",
                       "beneficiary name", "name of account holder"),
    "vendor_name": ("vendor name", "supplier name", "payee name"),
    "tax_number_1": ("tax number 1",),
    "payment_method": ("payment method",),
    "currency": ("order currency", "currency"),
}

_MIN_HITS_FOR_FORM = 3


def _lkey(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "X" if v else ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse(path: Path) -> tuple[dict, dict, int]:
    """-> (fields, provenance {canon: 'Sheet!R{r}C{c}'}, label_hits).
    fields carries EVERY canon key ('' when the label was found empty or the
    label is absent); label_hits counts labels FOUND (form detection)."""
    import openpyxl
    fields = {k: "" for k in LABEL_ALIASES}
    prov: dict[str, str] = {}
    hits = 0
    alias_map = {_lkey(a): canon
                 for canon, aliases in LABEL_ALIASES.items() for a in aliases}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
                for c, cell in enumerate(row):
                    canon = alias_map.get(_lkey(cell))
                    if not canon:
                        continue
                    if prov.get(canon):          # first label occurrence wins
                        continue
                    hits += 1
                    value = ""
                    for off in range(1, 4):      # value sits up to 3 cells right
                        if c + off < len(row):
                            v = _cell_str(row[c + off])
                            if v:
                                value = v
                                break
                    fields[canon] = value
                    prov[canon] = f"{ws.title}!R{r}C{c + 1}"
    finally:
        wb.close()
    return fields, prov, hits


def looks_like_request_form(path: Path) -> bool:
    """>=3 known labels anywhere in the workbook — shared with the D7
    container path's error message."""
    try:
        _, _, hits = parse(path)
    except Exception:
        return False
    return hits >= _MIN_HITS_FOR_FORM


def to_sap_fields(fields: dict) -> dict:
    """Template fields -> the SAP_KEYS shape sap_compare.compare consumes.
    The vendor name doubles as the account holder when the form has no
    dedicated holder line (the Americas form doesn't)."""
    return {
        "bank_details_id": "",
        "bank_account": fields.get("bank_account", ""),
        "account_holder": fields.get("account_holder", "")
                          or fields.get("vendor_name", ""),
        "account_name": "",
        "iban": fields.get("iban", ""),
        "control_key": "",
        "reference_details": "",
        "bank_country": fields.get("bank_country", ""),
        "bank_key": fields.get("bank_key", ""),
        "bank_name": fields.get("bank_name", ""),
        "street": "", "city": "", "bank_branch": "",
        "swift_bic": fields.get("swift_bic", ""),
    }


def register_secrets(ext, fields: dict) -> None:
    """Template values are secrets exactly like SAP values; Tax Number 1 is a
    TIN (masked under EVERY policy)."""
    for k in ("iban", "bank_account", "bank_key"):
        v = str(fields.get(k) or "")
        if v:
            kind = "iban" if k == "iban" else "account_number"
            ext.vault.register(kind, v)
    tin = str(fields.get("tax_number_1") or "")
    if tin:
        ext.vault.register("tin", tin)
