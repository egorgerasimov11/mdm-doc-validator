"""merge.py — merge ONE request form + ONE validated document into one vendor.

Form = base (name/address/company codes/purchasing orgs/payment terms/account
group + its own bank/tax fields). Document = authoritative for its own fields
(bank doc → BUT0BK bank fields; W-9 → LFA1.STCD/DFKKBPTAXNUM/legal name).

When form and document disagree, OR the document offers several routings (ACH
vs wires), the operator resolves each field with a button quiz (document is the
recommended default). The CHOICE is stored as a source-key, never the raw
value, so private account/IBAN numbers never sit in case JSON — the raw value
is re-derived from the document at consolidate time. routing is a public
identifier and is never masked.

The result's `rows` (raw, for consolidate/verify) is in-memory only; the route
stores just the masked `display` (candidates/cross_check) + `chosen`/`choices`.
"""
from __future__ import annotations

from pathlib import Path

from .. import runstore
from . import convert, docrows
from .template_io import cell_text

# BUT0BK techs the document can be authoritative for
_BANK_SHEET = "BUT0BK - Bank Account"
_NON_ROUTING_BANK = (
    ("BANKS", "BANKS", "Bank Country"),
    ("BANKN", "BANKN", "Bank Account"),
    ("IBAN", "IBAN", "IBAN"),
    ("KOINH", "KOINH", "Account Holder"),
)
_MASK_KIND = {"BANKN": "account_number", "IBAN": "iban", "TAX": "tin"}


def _disp(key: str, value: str) -> str:
    """Display value: routing (BANKL) and names are public → full; account /
    IBAN / TIN are masked. The stored merge carries only these displays."""
    value = cell_text(value)
    kind = _MASK_KIND.get(key)
    if value and kind:
        from ..privacy import mask
        return mask(kind, value)
    return value


def _mk_candidates(doc_opts: list[dict], form_val: str) -> list[dict]:
    """Dedup candidate values, document-first (so the default is the document).
    doc_opts: [{source, label, value}]; form_val is the form's normalized value."""
    cands: list[dict] = []
    seen: set[str] = set()
    for o in doc_opts:
        v = cell_text(o.get("value"))
        if v and v not in seen:
            cands.append({"source": o["source"], "label": o["label"], "value": v})
            seen.add(v)
    fv = cell_text(form_val)
    if fv and fv not in seen:
        cands.append({"source": "form", "label": "form", "value": fv})
        seen.add(fv)
    return cands


def _bank_specs(form_bank: dict, run_id: str) -> list[dict]:
    specs = []
    # routing: possibly several document candidates (ACH / wires) + the form's
    routing = docrows.routing_candidates(run_id)
    fv = form_bank.get("BANKL", "")
    specs.append(_spec("BANKL", "Bank Key", _mk_candidates(routing, fv), fv))
    doc_bank = docrows.doc_bank_fields(run_id)
    for key, _tech, label in _NON_ROUTING_BANK:
        fv = form_bank.get(key, "")
        doc_opts = [{"source": "doc", "label": "document", "value": doc_bank.get(key, "")}]
        specs.append(_spec(key, label, _mk_candidates(doc_opts, fv), fv))
    return specs


def _w9_specs(form_rows: dict, doc_run_id: str) -> list[dict]:
    ext = runstore.load(doc_run_id, "extraction.json") or {}
    f = ext.get("fields", {})
    form_lfa1 = (form_rows.get("LFA1 - Supplier General") or [{}])[0]
    doc_tin = docrows._value(f, "tin_raw" if "tin_raw" in f else "tin")
    doc_name = docrows._value(f, "line1_name")
    form_tax = form_lfa1.get("STCD1") or form_lfa1.get("STCD2") or ""
    form_name = form_lfa1.get("NAME1", "")
    specs = [
        _spec("TAX", "Tax Number",
              _mk_candidates([{"source": "doc", "label": "document", "value": doc_tin}], form_tax),
              form_tax),
        _spec("NAME", "Name",
              _mk_candidates([{"source": "doc", "label": "document", "value": doc_name}], form_name),
              form_name),
    ]
    return specs


def _spec(key: str, label: str, candidates: list[dict], form_val: str) -> dict:
    distinct = {c["value"] for c in candidates}
    return {"key": key, "label": label, "candidates": candidates,
            "form_val": cell_text(form_val),
            "ambiguous": len(distinct) > 1,
            "default_source": candidates[0]["source"] if candidates else None}


def _resolved(spec: dict, choices: dict) -> dict | None:
    """The chosen candidate (operator's pick, else the document-first default)."""
    if not spec["candidates"]:
        return None
    want = choices.get(spec["key"]) or spec["default_source"]
    for c in spec["candidates"]:
        if c["source"] == want:
            return c
    return spec["candidates"][0]


def merge_form_and_run(form_path: Path, run_id: str, columns, *, source_id: str,
                       region: str = "auto", country: str = "auto",
                       choices: dict | None = None) -> dict:
    """Merge result (build_rows superset). `choices`: {field_key: source_key}."""
    from sap_vendor_autoload.migrate import with_template_metadata
    choices = choices or {}

    built = convert.build_vendor_rows(form_path, columns, source_id=source_id,
                                      region=region, country=country)
    if built["errors"]:
        return {**built, "doc_class": "", "candidates": {}, "ambiguous_fields": [],
                "cross_check": [], "chosen": {}, "fills": []}

    ext = runstore.load(run_id, "extraction.json") or {}
    doc_class = ext.get("doc_class", "")
    rows = {s: [dict(r) for r in rs] for s, rs in built["rows"].items()}
    warnings = list(built["warnings"])

    if doc_class == "bank":
        docbuilt = docrows.rows_from_run(run_id, columns, source_id=source_id)
        if docbuilt["errors"]:
            return {**built, "rows": {}, "errors": docbuilt["errors"],
                    "doc_class": doc_class, "candidates": {}, "ambiguous_fields": [],
                    "cross_check": [], "chosen": {}, "fills": []}
        form_bank = (rows.get(_BANK_SHEET) or [{}])[0]
        specs = _bank_specs(form_bank, run_id)
    elif doc_class == "w9":
        docbuilt = docrows.rows_from_run(run_id, columns, source_id=source_id)
        if docbuilt["errors"]:
            return {**built, "rows": {}, "errors": docbuilt["errors"],
                    "doc_class": doc_class, "candidates": {}, "ambiguous_fields": [],
                    "cross_check": [], "chosen": {}, "fills": []}
        specs = _w9_specs(rows, run_id)
    else:
        return {**built, "rows": {}, "errors": [f"doc_class {doc_class!r} cannot be merged"],
                "doc_class": doc_class, "candidates": {}, "ambiguous_fields": [],
                "cross_check": [], "chosen": {}, "fills": []}

    candidates_disp: dict = {}
    cross_check: list = []
    chosen_src: dict = {}
    fills: list = []

    for spec in specs:
        key, label = spec["key"], spec["label"]
        res = _resolved(spec, choices)
        chosen_val = res["value"] if res else ""
        chosen_src[key] = res["source"] if res else None
        doc_cs = [c for c in spec["candidates"] if c["source"] != "form"]
        form_val = spec["form_val"]      # raw form value (candidates dedup it away on a match)
        doc_primary = doc_cs[0]["value"] if doc_cs else ""

        candidates_disp[key] = [
            {"source": c["source"], "label": c["label"], "display": _disp(key, c["value"]),
             "chosen": c["source"] == chosen_src[key]} for c in spec["candidates"]]

        # cross-check row for the divergence table
        if form_val and doc_primary:
            status = "match" if form_val == doc_primary else "MISMATCH"
        elif doc_primary and not form_val:
            status = "document-only"
            fills.append(key)
        elif form_val and not doc_primary:
            status = "form-only"
        else:
            status = "blank"
        cross_check.append({
            "field": label, "form": _disp(key, form_val),
            "document": _disp(key, doc_primary)
            + (f"  (+{len(doc_cs) - 1} more)" if len(doc_cs) > 1 else ""),
            "status": status, "ambiguous": spec["ambiguous"]})

        # audit + apply
        if chosen_val and form_val and chosen_val != form_val:
            warnings.append(
                f"{label}: written {_disp(key, chosen_val)} ({res['label']}); "
                f"form had {_disp(key, form_val)}")
        _apply(rows, doc_class, key, res, run_id, columns, source_id,
               with_template_metadata)

    ambiguous_fields = [s["key"] for s in specs if s["ambiguous"]]
    return {
        "rows": rows, "errors": [], "warnings": warnings,
        "unmapped": built["unmapped"], "partner_id": built["partner_id"],
        "source_id": source_id, "doc_class": doc_class,
        "candidates": candidates_disp, "ambiguous_fields": ambiguous_fields,
        "cross_check": cross_check, "chosen": chosen_src, "fills": fills,
    }


def _apply(rows, doc_class, key, res, run_id, columns, source_id, with_meta):
    """Overlay the chosen value onto the merged rows (document-authoritative)."""
    if res is None or not res["value"]:
        return
    val = res["value"]
    if doc_class == "bank":
        bank = (rows.get(_BANK_SHEET) or [{}])[0]
        new = dict(bank)
        new.setdefault("BKVID", "0001")
        new[key] = val
        rows[_BANK_SHEET] = [with_meta(columns, _BANK_SHEET, new, source_id)]
        return
    # w9
    if key == "NAME":
        for sheet, tech in (("LFA1 - Supplier General", "NAME1"),
                            ("BUT000 - General", "NAME_ORG1"),
                            ("ADRC - Address", "NAME1")):
            r = (rows.get(sheet) or [{}])[0]
            if r:
                r[tech] = val
                rows[sheet] = [r]
    elif key == "TAX" and res["source"] != "form":
        # the document decides the STCD column + DFKKBPTAXNUM; lift them from
        # the doc's own rows_from_run output so the type (SSN/EIN) is correct
        docbuilt = docrows.rows_from_run(run_id, columns, source_id=source_id)
        doc_lfa1 = (docbuilt["rows"].get("LFA1 - Supplier General") or [{}])[0]
        lfa1 = (rows.get("LFA1 - Supplier General") or [{}])[0]
        for t in ("STCD1", "STCD2"):
            lfa1.pop(t, None)
            if doc_lfa1.get(t):
                lfa1[t] = doc_lfa1[t]
        rows["LFA1 - Supplier General"] = [lfa1]
        if docbuilt["rows"].get("DFKKBPTAXNUM - Tax Number"):
            rows["DFKKBPTAXNUM - Tax Number"] = docbuilt["rows"]["DFKKBPTAXNUM - Tax Number"]
