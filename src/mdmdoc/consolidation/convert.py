"""convert.py — bridge to the sap-vendor-autoload mapping engine.

All sap_vendor_autoload imports live inside functions: the package is an
optional editable install (like address-validator), and this module must
import cleanly when it is absent — available() in __init__ gates the routes.
"""
from __future__ import annotations

from pathlib import Path

from .template_io import cell_text


def extract_form(path: Path) -> dict:
    """Provenance-rich extraction of a vendor request form:
    {"profile": {...}, "fields": {label: {"source_ref": "Sheet!Cell", "value": str}}}."""
    from sap_vendor_autoload.codex_review import build_source_extract
    return build_source_extract(path)


def build_vendor_rows(path: Path, columns, *, source_id: str | None = None,
                      region: str = "auto", country: str = "auto") -> dict:
    """Fresh parse of the form -> build_rows() result (rows/errors/warnings/
    unmapped/profile/partner_id/source_id). `columns` is a BPTemplate (or any
    column_for/has_sheet provider). tax_rows=True is Egor's consolidation
    decision (DFKKBPTAXNUM alongside LFA1.STCD); the converter default stays
    off so the CLI/wrapper autoload flow is unchanged."""
    from sap_vendor_autoload.migrate import build_rows
    from sap_vendor_autoload.reader import SourceForm

    src = SourceForm(path)
    try:
        return build_rows(src, columns, region=region, country=country,
                          source_id=source_id, tax_rows=True)
    finally:
        src.close()


def looks_like_vendor_form(path: Path) -> bool:
    from ..template_form import looks_like_request_form
    return looks_like_request_form(Path(path))


def mapping_targets() -> dict[str, list[str]]:
    """label -> ["SHEET.TECH", ...] from the converter's static maps, for the
    review screen's 'where will this land' column."""
    from sap_vendor_autoload.field_map import (
        COMPANY_CODES,
        FIELD_MAP,
        LFB1_BROADCASTS,
        LFM1_BROADCASTS,
        PURCHASING_ORGS,
        BankBlock,
        StateProvince,
    )

    targets: dict[str, list[str]] = {}

    def add(label, sheet, tech):
        targets.setdefault(label, []).append(f"{_short(sheet)}.{tech}")

    for sf in FIELD_MAP:
        for sheet, tech, _ in sf.targets:
            add(sf.label, sheet, tech)
    sp = StateProvince()
    for sheet, tech in sp.targets:
        add(sp.label, sheet, tech)
    for label, tech in (
        ("Bank Country", "BANKS"), ("Bank Key", "BANKL"),
        ("Bank Account Number", "BANKN"), ("IBAN", "IBAN"),
        ("Bank Account Holder", "KOINH"),
    ):
        add(label, "BUT0BK - Bank Account", tech)
    for b in (*LFB1_BROADCASTS, *LFM1_BROADCASTS):
        add(b.label, b.target_sheet, b.target_column)
    add(COMPANY_CODES.label, COMPANY_CODES.target_sheet, COMPANY_CODES.target_column)
    add(PURCHASING_ORGS.label, PURCHASING_ORGS.target_sheet, PURCHASING_ORGS.target_column)
    for label in ("Tax Number 1", "Tax Number 2", "Tax Number 3",
                  "Tax Number 4", "VAT Reg No"):
        add(label, "DFKKBPTAXNUM - Tax Number", "TAXNUM")
    return targets


def _short(sheet: str) -> str:
    return sheet.split(" - ")[0]


def _candidate_values(label: str, value: str) -> set[str]:
    """Every value the converter could legitimately have written for this
    source field — the raw string plus its known transforms."""
    from sap_vendor_autoload.transforms import (
        clean_iban,
        currency_code,
        extract_code,
        extract_company_codes,
        extract_purch_orgs,
        iso_country,
        language_code,
        payment_method_code,
        recipient_type_code,
    )

    out = {value, value.upper()}
    for fn in (extract_code, iso_country, language_code, currency_code,
               payment_method_code, recipient_type_code, clean_iban):
        try:
            v = fn(value)
        except Exception:
            v = None
        if v:
            out.add(cell_text(v))
    for fn in (extract_company_codes, extract_purch_orgs):
        try:
            out.update(fn(value))
        except Exception:
            pass
    return {v for v in out if v}


def coverage(extract_fields: dict, rows_by_sheet: dict,
             unmapped: list) -> list[dict]:
    """Per source field: did its value reach the plan, is it audit-only by
    design, or does it need operator eyes? Statuses follow the autoload
    wrapper taxonomy: uploaded / audit_only_by_design / not_loaded."""
    planned_values = set()
    for rows in rows_by_sheet.values():
        for row in rows:
            for v in row.values():
                s = cell_text(v)
                if s:
                    planned_values.add(s)

    audit_labels = {label for (_s, _c, label, _r, _v) in unmapped}
    targets = mapping_targets()

    out = []
    for label, info in extract_fields.items():
        value = cell_text(info.get("value"))
        entry = {
            "label": label,
            "value": _display(label, value),
            "source_ref": info.get("source_ref", ""),
            "target": ", ".join(targets.get(label, [])) or "—",
        }
        if not value:
            entry["status"] = "blank_source"
        elif label in audit_labels:
            entry["status"] = "audit_only_by_design"
        elif _candidate_values(label, value) & planned_values:
            entry["status"] = "uploaded"
        else:
            entry["status"] = "not_loaded"
        out.append(entry)
    return out


# Private identifiers are masked in coverage/case JSON under EVERY policy; the
# full value's only home is the output workbook. routing/bank key is a PUBLIC
# identifier (Egor's decision) and stays full.
_MASK_LABELS = {
    "Tax Number 1": "tin", "Tax Number 2": "tin",
    "Tax Number 3": "tin", "Tax Number 4": "tin",
    "Bank Account Number": "account_number", "Bank Account": "account_number",
    "IBAN": "iban", "IBAN Number": "iban",
}


def _display(label: str, value: str) -> str:
    value = cell_text(value)
    kind = _MASK_LABELS.get(label)
    if value and kind:
        from ..privacy import mask
        return mask(kind, value)
    return value


def masked_extract(extract: dict) -> dict:
    """The storable form of a source extract: TIN-labeled values masked.
    Rows for the workbook are always re-derived from the source file, so the
    stash never needs the full numbers."""
    fields = {}
    for label, info in (extract.get("fields") or {}).items():
        v = cell_text(info.get("value"))
        fields[label] = {**info, "value": _display(label, v)}
    return {**extract, "fields": fields}
