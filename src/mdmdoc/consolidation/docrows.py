"""docrows.py — Task 1: a validated document run becomes consolidation rows.

A bank document or W-9 alone is a vendor FRAGMENT (bank details + tax
numbers + names), not a full vendor record — it enters a consolidation case
as kind="doc" and gets its own SOURCE_ID, or (when the run was cross-checked
against an attached request form with zero mismatches) the form itself is
extracted as the full vendor.

Values come from the run's persisted extraction.json. On the operator console
the artifacts carry full values (bank_values_policy()=="full"); on masked
deployments the export refuses with a clear message instead of writing masked
placeholders into a SAP upload.
"""
from __future__ import annotations

import re

from .. import config, runstore

_EXPORT_CLASSES = ("bank", "w9")


def _value(fields: dict, key: str) -> str:
    """Full value of one extraction field; '' when absent or masked-only."""
    v = fields.get(key)
    if isinstance(v, dict):
        return str(v.get("value") or "").strip()
    if v is None:
        return ""
    return str(v).strip()


def _masked_only(fields: dict, key: str) -> bool:
    v = fields.get(key)
    return isinstance(v, dict) and bool(v.get("present")) and not v.get("value")


def _gate_common(run_id: str, confirm: bool) -> tuple[bool, str]:
    """Shared verdict/doc_class/policy gate for both export (Task 1) and
    attach (form+doc merge). ACCEPT free; WARNING needs the operator's
    explicit confirm; REVIEW/REJECT never; artifacts must be full-policy."""
    report = runstore.load(run_id, "report.json")
    if not report:
        return False, "run has no report artifact"
    if report.get("doc_class") not in _EXPORT_CLASSES:
        return False, f"doc_class {report.get('doc_class')!r} has nothing to consolidate"
    verdict = report.get("verdict", "")
    if verdict == "WARNING" and not confirm:
        return False, "verdict is WARNING — tick the confirm checkbox to use it anyway"
    if verdict not in ("ACCEPT", "WARNING"):
        return False, f"verdict {verdict!r} — fix the findings before consolidating"
    if config.bank_values_policy() != "full":
        return False, ("run artifacts are masked on this deployment — analyze "
                       "from the operator console (MDMDOC_BANK_VALUES=full)")
    return True, ""


def export_gate(run_id: str, confirm: bool = False) -> tuple[bool, str]:
    """Task 1 gate: _gate_common PLUS a hard block on any document-vs-attached-
    form MISMATCH the run already recorded (that path writes the form as the
    full vendor, so a mismatch must not slip through)."""
    ok, reason = _gate_common(run_id, confirm)
    if not ok:
        return ok, reason
    tpl_rows = runstore.load(run_id, "template_compare.json") or []
    mismatches = [r["field"] for r in tpl_rows
                  if str(r.get("status", "")).upper() == "MISMATCH"]
    if mismatches:
        return False, ("document vs request-form MISMATCH on "
                       f"{', '.join(mismatches)} — resolve before consolidating")
    return True, ""


def attach_gate(run_id: str, confirm: bool = False) -> tuple[bool, str]:
    """Merge gate: _gate_common only. Form-vs-document divergence is NOT a hard
    block here — it is surfaced as the merge cross-check + the operator's
    per-field quiz (document-recommended), then confirm_warnings."""
    return _gate_common(run_id, confirm)


# a bank document can legitimately carry more than one routing number
# (ACH vs wires); each is a candidate for the single SAP BANKL cell.
ROUTING_SOURCES = (
    ("doc_ach", "routing_aba", "document — ACH"),
    ("doc_wires", "routing_aba_wires", "document — wires"),
)


def routing_candidates(run_id: str) -> list[dict]:
    """[{source, field, label, value}] — every non-blank bank-key candidate the
    document holds: US ABA routings (ACH / wires) AND the national clearing
    code, which IS the SAP Bank Key for CN (CNAPS 联行号) / GB (sort) / AU (BSB)
    / IN (IFSC). routing/bank-key is a PUBLIC identifier: never masked."""
    ext = runstore.load(run_id, "extraction.json") or {}
    fields = ext.get("fields", {})
    out = []
    for source, field, label in ROUTING_SOURCES:
        v = _value(fields, field)
        if v:
            out.append({"source": source, "field": field, "label": label,
                        "value": v})
    nc = _value(fields, "national_clearing")
    if nc:
        kind = _value(fields, "national_clearing_kind") or "clearing"
        out.append({"source": "doc_clearing", "field": "national_clearing",
                    "label": f"document — {kind}", "value": nc})
    return out


def doc_bank_fields(run_id: str) -> dict:
    """The document's authoritative bank values for cross-check/candidates
    (full values; routing public, account/IBAN masked only at display time)."""
    from sap_vendor_autoload.transforms import clean_iban, iso_country
    ext = runstore.load(run_id, "extraction.json") or {}
    fields = ext.get("fields", {})
    return {
        "BANKS": iso_country(_value(fields, "bank_country")) or "",
        "BANKN": _value(fields, "account_number"),
        "IBAN": clean_iban(_value(fields, "iban")) or "",
        "KOINH": _value(fields, "account_holder"),
    }


def attached_form_path(run_id: str):
    """The request form this run was compared against, when still on disk —
    zero-mismatch runs consolidate the FORM (full vendor), the document
    having corroborated it."""
    from pathlib import Path
    meta = runstore.load(run_id, "meta.json") or {}
    p = meta.get("template_path")
    if not p:
        return None
    path = Path(p)
    return path if path.exists() else None


_CSZ_RE = re.compile(r"^(?P<city>.+?)[,\s]+(?P<region>[A-Z]{2})[,\s]+(?P<zip>\d{5}(?:-\d{4})?)$")
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_EIN_RE = re.compile(r"^\d{2}-\d{7}$")


def rows_from_run(run_id: str, columns, *, source_id: str) -> dict:
    """build_rows()-shaped result for a document run."""
    from sap_vendor_autoload.migrate import with_template_metadata
    from sap_vendor_autoload.transforms import clean_iban, iso_country

    ext = runstore.load(run_id, "extraction.json") or {}
    fields = ext.get("fields", {})
    doc_class = ext.get("doc_class", "")
    rows: dict[str, list] = {}
    warnings: list[str] = []
    errors: list[str] = []

    def emit(sheet, row_fields):
        clean = {k: v for k, v in row_fields.items() if v}
        if not clean:
            return
        rows.setdefault(sheet, []).append(
            with_template_metadata(columns, sheet, clean, source_id))

    if doc_class == "bank":
        for key in ("account_number", "routing_aba", "iban"):
            if _masked_only(fields, key):
                errors.append(f"{key} is masked in the run artifact — "
                              "re-analyze on the operator console first")
        if errors:
            return _result(rows, errors, warnings, source_id)
        holder = _value(fields, "account_holder")
        emit("BUT0BK - Bank Account", {
            "BKVID": "0001",
            "BANKS": iso_country(_value(fields, "bank_country")),
            "BANKL": _value(fields, "routing_aba"),
            "BANKN": _value(fields, "account_number"),
            "KOINH": holder,
            "IBAN": clean_iban(_value(fields, "iban")),
        })
        if _value(fields, "swift_bic"):
            warnings.append("SWIFT/BIC stays audit-only — the BP template has "
                            "no BUT0BK column for it")
        if not rows:
            errors.append("no bank fields extracted — nothing to consolidate")

    elif doc_class == "w9":
        # older artifacts stored the TIN under "tin"; current schema uses "tin_raw"
        tin_key = "tin_raw" if "tin_raw" in fields else "tin"
        if _masked_only(fields, tin_key):
            errors.append("TIN is masked in this run's artifact (old or "
                          "masked-policy run) — Re-analyze the document on "
                          "the operator console, then export")
        if errors:
            return _result(rows, errors, warnings, source_id)
        name = _value(fields, "line1_name")
        business = _value(fields, "line2_business_name")
        tin = _value(fields, tin_key)
        tin_type = _value(fields, "tin_type").upper()
        if not tin_type and tin:
            # infer from shape rather than defaulting: EIN XX-XXXXXXX vs
            # SSN XXX-XX-XXXX — filing an EIN under STCD1/US1 would be wrong
            if _EIN_RE.fullmatch(tin):
                tin_type = "EIN"
            elif _SSN_RE.fullmatch(tin):
                tin_type = "SSN"
        stcd = {}
        taxtype = ""
        if tin:
            if tin_type == "EIN":
                stcd["STCD2"], taxtype = tin, "US2"
            else:
                stcd["STCD1"], taxtype = tin, "US1"
                if tin_type != "SSN":
                    warnings.append(
                        f"TIN type unclear (tin_type={tin_type or 'blank'!r}, "
                        f"shape unrecognized) — mapped to STCD1/US1 like an "
                        "SSN; check before import")
        street = _value(fields, "address_street")
        csz = _value(fields, "address_city_state_zip")
        adr = {}
        m = _CSZ_RE.match(csz) if csz else None
        if m:
            adr = {"city": m.group("city").strip(), "region": m.group("region"),
                   "zip": m.group("zip")}
        elif csz:
            warnings.append(f"could not split city/state/ZIP {csz!r} — "
                            "address left off the upload, fill manually")
        emit("LFA1 - Supplier General", {
            "NAME1": name, "NAME2": business,
            "LAND1": "US",  # a W-9 is a US-person certification
            "STRAS": street, "ORT01": adr.get("city"),
            "REGIO": adr.get("region"), "PSTLZ": adr.get("zip"),
            **stcd,
        })
        emit("BUT000 - General", {"NAME_ORG1": name, "NAME_ORG2": business})
        emit("ADRC - Address", {
            "NAME1": name, "STREET": street, "CITY1": adr.get("city"),
            "REGION": adr.get("region"), "POST_CODE1": adr.get("zip"),
            "COUNTRY": "US",
        })
        has_sheet = getattr(columns, "has_sheet", None)
        if tin and taxtype and (has_sheet is None or has_sheet("DFKKBPTAXNUM - Tax Number")):
            emit("DFKKBPTAXNUM - Tax Number", {"TAXTYPE": taxtype, "TAXNUM": tin})
        if not name and not tin:
            errors.append("neither a name nor a TIN extracted — nothing to consolidate")

    else:
        errors.append(f"doc_class {doc_class!r} has nothing to consolidate")

    return _result(rows, errors, warnings, source_id)


def _result(rows, errors, warnings, source_id):
    return {"rows": {} if errors else rows, "errors": errors,
            "warnings": warnings, "unmapped": [],
            "partner_id": None, "source_id": source_id}


def coverage_for_run(run_id: str, built: dict) -> list[dict]:
    """Display rows for the case page, mirroring the form-extract coverage:
    which artifact fields landed where. Values shown as stored in the run
    artifact (already policy-masked at run time where applicable)."""
    ext = runstore.load(run_id, "extraction.json") or {}
    fields = ext.get("fields", {})
    prov = ext.get("provenance", {})
    planned = {}
    for sheet, sheet_rows in built.get("rows", {}).items():
        for row in sheet_rows:
            for tech, v in row.items():
                planned.setdefault(str(v), []).append(
                    f"{sheet.split(' - ')[0]}.{tech}")
    out = []
    for key, raw in fields.items():
        if isinstance(raw, dict):
            # sensitive field: JSON/display side always carries the MASKED
            # form; the full value's only home is the output workbook
            value = str(raw.get("value") or "")
            shown = str(raw.get("masked") or value)
        else:
            value = shown = str(raw or "")
        if not value and not shown:
            continue
        p = prov.get(key) or {}
        ref = p.get("source", "")
        if p.get("page"):
            ref += f" p{p['page']}"
        targets = planned.get(value, []) if value else []
        out.append({
            "label": key,
            "value": shown,
            "source_ref": ref,
            "target": ", ".join(targets) or "—",
            "status": "uploaded" if targets else "audit_only_by_design",
        })
    return out
