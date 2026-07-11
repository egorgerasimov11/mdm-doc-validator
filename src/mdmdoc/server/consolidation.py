#!/usr/bin/env python3
"""server/consolidation.py — the Consolidation tab.

Vendor request workbooks (Americas/EU/APAC MDM templates) -> appended rows in
the operator's BusinessPartnerTemplate.xlsx. Server-rendered pages + sync
form-POSTs (the /ui/tax pattern): the whole extract+append+verify cycle is
sub-second on real files, so no jobs machinery.

Safety model: rows are built by the sap-vendor-autoload converter, appended
by consolidation.template_io (append-only, never mutates the upload), and
every output is re-proven by consolidation.verify (passes B/C/D) before the
download link activates. Nothing is written when pass A (plan review) errors,
when warnings lack the operator's explicit confirm, or when a duplicate
vendor lacks the explicit override.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import runstore
from ..consolidation import available, casestore, convert, docrows
from ..consolidation import merge as mergemod
from ..consolidation import plan as planmod
from ..consolidation import source_id as sidmod
from ..consolidation.template_io import BPTemplate, BPTemplateError, REQUIRED_SHEETS, normalized_name
from ..consolidation.verify import verify_output
from .deps import save_upload

router_consol = APIRouter(include_in_schema=False)
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates" / "ui"))

_FORM_EXTS = (".xlsx", ".xlsm")


class _PreviewColumns:
    """Column provider for extract-time previews, before any template exists:
    pretends every sheet and column is present so build_rows can run. The
    real template replaces it at consolidate time."""

    def has_sheet(self, sheet):
        return True

    def column_for(self, sheet, tech):
        return 1


def _ctx(**kw):
    from .ui import _ctx as ui_ctx  # lazy: ui.py includes this router at import
    return ui_ctx(page="consolidation", **kw)


def _load_case_or_404(case_id: str) -> dict:
    try:
        case = casestore.load_case(case_id)
    except ValueError:
        raise HTTPException(404)
    if case is None:
        raise HTTPException(404)
    return case


def _vendor_views(case: dict) -> list[dict]:
    out = []
    for v in case["vendors"]:
        ex = casestore.load_extract(case["case_id"], v["vkey"]) or {}
        merge = v.get("merge") or {}
        unresolved = [k for k in merge.get("ambiguous_fields", [])
                      if k not in (merge.get("choices") or {})]
        out.append({**v, "coverage": ex.get("coverage", []),
                    "unresolved": unresolved})
    return out


def _attach_candidates() -> list[dict]:
    """bank/W-9 runs eligible to attach (ACCEPT/WARNING), newest first."""
    rows = runstore.list_runs(test=False)
    out = []
    for r in rows:
        report = runstore.load(r.get("run_id", ""), "report.json") or {}
        if report.get("doc_class") in ("bank", "w9") and \
                report.get("verdict") in ("ACCEPT", "WARNING"):
            out.append({"run_id": r.get("run_id"),
                        "label": r.get("file_name") or r.get("run_id"),
                        "doc_class": report.get("doc_class"),
                        "verdict": report.get("verdict")})
    out.sort(key=lambda r: r["run_id"])
    return out[:50]


def _unresolved_quiz(case: dict) -> list[str]:
    """Vendor labels whose merge still has an ambiguous field the operator
    has not explicitly resolved — consolidation is blocked until they do."""
    blocked = []
    for v in case["vendors"]:
        if v.get("status") != "extracted":
            continue
        merge = v.get("merge") or {}
        choices = merge.get("choices") or {}
        if any(k not in choices for k in merge.get("ambiguous_fields", [])):
            blocked.append(v["label"])
    return blocked


def _latest_report(case: dict) -> dict | None:
    if not case.get("outputs"):
        return None
    name = case["outputs"][-1]["name"]
    p = casestore.case_dir(case["case_id"]) / f"{name}.verify.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _recompute_merge(vendor: dict) -> None:
    """Run the merge with the vendor's stored choices and cache the DISPLAY
    parts (masked candidates / cross_check / chosen — never raw values) onto
    the vendor record. Raw rows are re-derived at consolidate time."""
    run_id = vendor["attached_run_id"]
    choices = (vendor.get("merge") or {}).get("choices", {})
    m = mergemod.merge_form_and_run(
        Path(vendor["source_path"]), run_id, _PreviewColumns(),
        source_id=vendor["source_id"], choices=choices)
    report = runstore.load(run_id, "report.json") or {}
    vendor["merge"] = {
        "run_id": run_id,
        "doc_class": m.get("doc_class", ""),
        "doc_label": (runstore.load(run_id, "meta.json") or {}).get("file_name", run_id),
        "verdict": report.get("verdict", ""),
        "errors": m.get("errors", []),
        "cross_check": m.get("cross_check", []),
        "candidates": m.get("candidates", {}),
        "ambiguous_fields": m.get("ambiguous_fields", []),
        "fills": m.get("fills", []),
        "choices": choices,
        "attached": datetime.now().isoformat(timespec="seconds"),
    }
    vendor["rows_summary"] = {s: len(r) for s, r in m.get("rows", {}).items()}
    vendor["warnings"] = m.get("warnings", vendor.get("warnings", []))


def _render_case(request: Request, case: dict, *, error: str = "",
                 needs_confirm: dict | None = None):
    dl = casestore.latest_verified_output(case)
    return templates.TemplateResponse(request, "consolidation_case.html", _ctx(
        case=case,
        vendors=_vendor_views(case),
        pending=[v for v in case["vendors"] if v["status"] == "extracted"],
        candidates=_attach_candidates(),
        error=error,
        needs_confirm=needs_confirm or {},
        report=_latest_report(case),
        download=dl,
    ))


@router_consol.get("/ui/consolidation", response_class=HTMLResponse)
def consolidation_page(request: Request):
    return templates.TemplateResponse(request, "consolidation.html", _ctx(
        cases=casestore.list_cases(),
        engine_ok=available(),
        default_tpl=casestore.bp_template_default(),
    ))


@router_consol.post("/ui/consolidation/new")
def consolidation_new():
    if not available():
        raise HTTPException(503, detail="sap_vendor_autoload is not installed")
    case = casestore.new_case()
    return RedirectResponse(f"/ui/consolidation/{case['case_id']}", status_code=303)


@router_consol.get("/ui/consolidation/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: str):
    return _render_case(request, _load_case_or_404(case_id))


@router_consol.post("/ui/consolidation/{case_id}/extract", response_class=HTMLResponse)
async def case_extract(request: Request, case_id: str):
    case = _load_case_or_404(case_id)
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return _render_case(request, case, error="no form uploaded")
    if not up.filename.lower().endswith(_FORM_EXTS):
        return _render_case(request, case,
                            error="a vendor request form is .xlsx/.xlsm")
    path = save_upload(up.filename, await up.read())
    if not convert.looks_like_vendor_form(path):
        return _render_case(request, case, error=(
            f"{up.filename!r} does not look like a vendor request form "
            "(no known field labels found)"))

    try:
        extract = convert.extract_form(path)
        built = convert.build_vendor_rows(path, _PreviewColumns())
    except Exception as exc:
        return _render_case(request, case, error=(
            f"extraction failed: {exc.__class__.__name__}: {exc}"))

    known_ids = set((case.get("template") or {}).get("existing_ids", []))
    known_ids |= {v["source_id"] for v in case["vendors"] if v.get("source_id")}
    sid = built["partner_id"] or sidmod.generate(known_ids)

    vkey = casestore.next_vkey(case)
    vendor = {
        "vkey": vkey,
        "kind": "form",
        "label": up.filename,
        "source_path": str(path),
        "source_id": sid,
        "status": "error" if built["errors"] else "extracted",
        "errors": built["errors"],
        "warnings": built["warnings"],
        "profile": built["profile"].to_dict(),
        "rows_summary": {s: len(r) for s, r in built["rows"].items()},
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    coverage = convert.coverage(extract["fields"], built["rows"], built["unmapped"])
    casestore.save_extract(case_id, vkey, {
        "extract": convert.masked_extract(extract), "coverage": coverage})
    case["vendors"].append(vendor)
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.post("/ui/consolidation/{case_id}/vendor/{vkey}/source-id",
                    response_class=HTMLResponse)
async def case_source_id(request: Request, case_id: str, vkey: str):
    case = _load_case_or_404(case_id)
    form = await request.form()
    sid = str(form.get("source_id") or "").strip()
    vendor = next((v for v in case["vendors"] if v["vkey"] == vkey), None)
    if vendor is None:
        raise HTTPException(404)
    if vendor["status"] != "extracted":
        return _render_case(request, case, error=(
            f"SOURCE_ID of a {vendor['status']} vendor cannot change — it is "
            "already part of a verified output (or failed extraction)"))
    known_ids = set((case.get("template") or {}).get("existing_ids", []))
    known_ids |= {v["source_id"] for v in case["vendors"]
                  if v["vkey"] != vkey and v.get("source_id")}
    probs = sidmod.problems(sid, known_ids)
    if probs:
        return _render_case(request, case, error="; ".join(probs))
    vendor["source_id"] = sid
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.post("/ui/consolidation/{case_id}/template", response_class=HTMLResponse)
async def case_template(request: Request, case_id: str):
    case = _load_case_or_404(case_id)
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return _render_case(request, case, error="no template uploaded")
    if not up.filename.lower().endswith(".xlsx"):
        return _render_case(request, case,
                            error="the BP consolidation template is .xlsx")
    if case.get("outputs") and form.get("confirm_replace") != "on":
        return _render_case(request, case, needs_confirm={"replace_template": True},
                            error=("this case already has consolidated output; "
                                   "replacing the template discards it — tick "
                                   "the confirmation to proceed"))
    clear_existing = form.get("clear_existing") == "on"
    path = save_upload(up.filename, await up.read())
    tpl = None
    try:
        tpl = BPTemplate(path)
        tpl.validate()
        info = {
            "orig_name": up.filename,
            "inbox_path": str(path),
            "uploaded": datetime.now().isoformat(timespec="seconds"),
        }
        if clear_existing:
            cleared = tpl.clear_data_rows()   # in-memory only; upload untouched
            # intermediate base: an emptied template has no data, so hiding empty
            # columns would hide everything; the final save_to recomputes it
            base = tpl.save_to(casestore.case_dir(case_id) / "base_cleared.xlsx",
                               hide_empty=False)
            info["base_path"] = str(base)
            info["cleared"] = cleared
        info["existing_ids"] = sorted(tpl.existing_source_ids())
        info["existing_names"] = sorted(tpl.existing_vendor_names())
        info["data_rows"] = {s: tpl.data_rows_used(s) for s in REQUIRED_SHEETS}
    except BPTemplateError as exc:
        return _render_case(request, case, error=str(exc))
    finally:
        if tpl is not None:
            tpl.close()
    case["template"] = info
    # discarding the accumulated output un-consolidates its vendors: their
    # rows lived only in the discarded files, so they go back to pending
    if case.get("outputs"):
        for v in case["vendors"]:
            if v["status"] == "consolidated":
                v["status"] = "extracted"
    case["outputs"] = []
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


def _rows_for_vendor(vendor: dict, columns, today: str | None = None) -> dict:
    """Re-derive one vendor's rows from its saved source — fresh parse every
    time (the stash is display-only). A form vendor with an attached document
    goes through the merge (form base + document-authoritative bank/tax fields,
    resolving each field by the operator's stored choice).

    When `columns` is a real BPTemplate and `today` is given, SAP-mandatory KEY
    fields the converter leaves blank (ADR2/ADRC DATE_FROM, …) are filled — the
    single seam so form/doc/merge and the Pass-D re-derivation stay identical."""
    if vendor["kind"] == "form":
        if vendor.get("attached_run_id"):
            built = mergemod.merge_form_and_run(
                Path(vendor["source_path"]), vendor["attached_run_id"], columns,
                source_id=vendor["source_id"],
                choices=(vendor.get("merge") or {}).get("choices", {}))
        else:
            built = convert.build_vendor_rows(
                Path(vendor["source_path"]), columns, source_id=vendor["source_id"])
    else:
        built = docrows.rows_from_run(vendor["run_id"], columns,
                                      source_id=vendor["source_id"])
    if today and not built.get("errors") and hasattr(columns, "key_fields"):
        from ..consolidation import address, fieldfit, required_fields
        required_fields.fill(built["rows"], columns.key_fields(), today)
        required_fields.apply_constants(built["rows"], columns)
        # address completeness (before fieldfit so the local-script row is fitted too)
        form_path = vendor["source_path"] if (
            vendor.get("kind") == "form" and vendor.get("source_path")) else None
        if form_path:
            address.add_international_version(built["rows"], form_path)
        address.assign_source_addrnumber(built["rows"])
        address.fill_region(built["rows"], built.get("warnings"))
        address.fill_phone_country(built["rows"], columns)
        if form_path:
            address.fill_district(built["rows"], form_path, columns)
        fieldfit.fit_sap_fields(built["rows"], columns)
    return built


@router_consol.post("/ui/consolidation/{case_id}/consolidate", response_class=HTMLResponse)
async def case_consolidate(request: Request, case_id: str):
    case = _load_case_or_404(case_id)
    form = await request.form()
    confirm_warnings = form.get("confirm_warnings") == "on"
    override_dup = form.get("override_dup") == "on"
    today = datetime.now().strftime("%Y%m%d")   # captured once → Pass D matches

    if not case.get("template"):
        return _render_case(request, case, error="upload the BP template first")
    pending = [v for v in case["vendors"] if v["status"] == "extracted"]
    if not pending:
        return _render_case(request, case, error="no extracted vendors to consolidate")

    # Fast mode (Egor): consolidate straight from the form(s), skip ALL checks —
    # only for form-only vendors (a document was attached to be verified).
    fast = form.get("fast_mode") == "on"
    if fast and any(v.get("attached_run_id") for v in pending):
        return _render_case(request, case, error=(
            "fast mode is for form-only vendors — detach the document(s) or "
            "uncheck fast mode to run the checks"))

    if not fast:
        blocked = _unresolved_quiz(case)
        if blocked:
            return _render_case(request, case, error=(
                "resolve the field choice(s) first (pick a value for the highlighted "
                f"bank fields): {', '.join(blocked)}"))

    last_ok = casestore.latest_verified_output(case)
    if last_ok:
        base = casestore.output_path(case_id, last_ok["name"])
        base_name = last_ok["name"]
    else:
        # a cleared template produces base_cleared.xlsx; the snapshot baseline
        # is therefore taken on the already-cleaned file
        base = Path(case["template"].get("base_path") or case["template"]["inbox_path"])
        base_name = case["template"]["orig_name"]

    tpl = None
    try:
        tpl = BPTemplate(base)
        tpl.validate()
        existing_ids = tpl.existing_source_ids()
        existing_names = tpl.existing_vendor_names()

        pre = tpl.snapshot()
        write_plan: list[dict] = []
        batch: list[dict] = []
        warnings_all: list[str] = []
        dups: list[str] = []

        for v in pending:
            built = _rows_for_vendor(v, tpl, today)
            if built["errors"]:
                return _render_case(request, case, error=(
                    f"{v['label']}: " + "; ".join(built["errors"])))
            sid = v["source_id"]
            if fast:
                # skip every check; just write the form's rows
                write_plan.extend(tpl.append_rows(built["rows"]))
                batch.append({"vkey": v["vkey"], "sid": sid})
                continue

            probs = sidmod.problems(sid, existing_ids)
            if probs:
                return _render_case(request, case, error=(
                    f"{v['label']}: " + "; ".join(probs)))

            lfa1 = built["rows"].get("LFA1 - Supplier General") or [{}]
            vname = normalized_name(lfa1[0].get("NAME1", ""))
            if vname and vname in existing_names:
                dups.append(f"{v['label']}: vendor {vname!r} already exists "
                            "in the template")
            if vname:
                # same-batch duplicates must trip the gate too, mirroring
                # existing_ids.add(sid) below
                existing_names.add(vname)

            cells = tpl.plan_rows(built["rows"])
            review = planmod.review(cells, kind=v["kind"], source_id=sid)
            if review["errors"]:
                return _render_case(request, case, error=(
                    f"{v['label']}: " +
                    "; ".join(e["message"] for e in review["errors"])))
            warnings_all.extend(
                f"{v['label']}: {w['message']}" for w in review["warnings"])
            warnings_all.extend(
                f"{v['label']}: {w}" for w in built["warnings"])

            write_plan.extend(tpl.append_rows(built["rows"]))
            existing_ids.add(sid)
            batch.append({"vkey": v["vkey"], "sid": sid})

        if not fast and dups and not override_dup:
            return _render_case(request, case,
                                needs_confirm={"dups": dups,
                                               "warnings": warnings_all},
                                error="duplicate vendor — nothing was written")
        if not fast and warnings_all and not confirm_warnings:
            return _render_case(request, case,
                                needs_confirm={"warnings": warnings_all,
                                               "dups": dups},
                                error=(f"{len(warnings_all)} warning(s) — "
                                       "nothing was written; confirm to proceed"))

        out_name = casestore.next_output_name(case)
        out_path = tpl.save_to(casestore.out_dir(case_id) / out_name)
    except BPTemplateError as exc:
        return _render_case(request, case, error=str(exc))
    finally:
        if tpl is not None:
            tpl.close()

    if fast:
        report = {"schema": "mdmdoc.consolidate.v1", "status": "skipped",
                  "passes": []}
    else:
        # independent re-derivation for pass D, against the OUTPUT's own columns
        fresh_by_vendor = {}
        out_tpl = BPTemplate(out_path)
        try:
            by_vkey = {v["vkey"]: v for v in case["vendors"]}
            for b in batch:
                fresh_by_vendor[b["sid"]] = _rows_for_vendor(
                    by_vkey[b["vkey"]], out_tpl, today)["rows"]
        finally:
            out_tpl.close()
        report = verify_output(out_path, write_plan, pre, fresh_by_vendor)

    casestore.save_verify(case_id, out_name, report)
    case["outputs"].append({
        "name": out_name,
        "base": base_name,
        "vendors": [b["vkey"] for b in batch],
        "verify_status": report["status"],
        "fast": fast,
        "override_dup": bool(dups),
        "confirmed_warnings": warnings_all if confirm_warnings else [],
        "cleared": (case["template"].get("cleared") if not last_ok else None),
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    if report["status"] in ("verified", "skipped"):
        done = {b["vkey"] for b in batch}
        for v in case["vendors"]:
            if v["vkey"] in done:
                v["status"] = "consolidated"
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.get("/ui/consolidation/attach/candidates")
def attach_candidates():
    # two path segments so it never collides with GET /ui/consolidation/{case_id}
    from fastapi.responses import JSONResponse
    return JSONResponse({"candidates": _attach_candidates()})


@router_consol.post("/ui/consolidation/{case_id}/vendor/{vkey}/attach-run",
                    response_class=HTMLResponse)
async def case_attach_run(request: Request, case_id: str, vkey: str):
    """Attach a validated bank/W-9 run to a form vendor → merge. The document
    is authoritative for its own bank/tax fields; divergences surface as the
    cross-check + per-field quiz on the case page."""
    case = _load_case_or_404(case_id)
    form = await request.form()
    run_id = str(form.get("run_id") or "").strip()
    confirm = form.get("confirm") == "on"
    vendor = next((v for v in case["vendors"] if v["vkey"] == vkey), None)
    if vendor is None:
        raise HTTPException(404)
    if vendor["kind"] != "form" or vendor["status"] != "extracted":
        return _render_case(request, case, error=(
            "a document can only be attached to an extracted request-form vendor"))
    if not runstore.load(run_id, "report.json"):
        return _render_case(request, case, error="unknown document run")
    ok, reason = docrows.attach_gate(run_id, confirm)
    if not ok:
        return _render_case(request, case, error=reason)
    vendor["attached_run_id"] = run_id
    vendor["merge"] = {"choices": {}}
    _recompute_merge(vendor)
    if vendor["merge"].get("errors"):
        vendor.pop("attached_run_id", None)
        vendor["merge"] = {}
        return _render_case(request, case, error=(
            "document could not be merged: "
            + "; ".join(vendor.get("merge", {}).get("errors", ["unknown"]))))
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.post("/ui/consolidation/{case_id}/vendor/{vkey}/resolve",
                    response_class=HTMLResponse)
async def case_resolve(request: Request, case_id: str, vkey: str):
    """Operator's quiz pick: {field_key: source_key} → stored choices, merge
    recomputed so the divergence table + rows_summary reflect the choice."""
    case = _load_case_or_404(case_id)
    form = await request.form()
    vendor = next((v for v in case["vendors"] if v["vkey"] == vkey), None)
    if vendor is None or not vendor.get("attached_run_id"):
        raise HTTPException(404)
    if vendor["status"] != "extracted":
        return _render_case(request, case, error="this vendor is already consolidated")
    merge = vendor.setdefault("merge", {"choices": {}})
    choices = dict(merge.get("choices") or {})
    valid_sources = {
        key: {c["source"] for c in cands}
        for key, cands in (merge.get("candidates") or {}).items()}
    for key in merge.get("ambiguous_fields", []):
        picked = str(form.get(f"choice_{key}") or "").strip()
        if picked and picked in valid_sources.get(key, set()):
            choices[key] = picked
    merge["choices"] = choices
    _recompute_merge(vendor)
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.post("/ui/consolidation/{case_id}/vendor/{vkey}/detach",
                    response_class=HTMLResponse)
async def case_detach(request: Request, case_id: str, vkey: str):
    case = _load_case_or_404(case_id)
    vendor = next((v for v in case["vendors"] if v["vkey"] == vkey), None)
    if vendor is None:
        raise HTTPException(404)
    if vendor["status"] != "extracted":
        return _render_case(request, case, error="this vendor is already consolidated")
    vendor.pop("attached_run_id", None)
    vendor["merge"] = {}
    built = convert.build_vendor_rows(
        Path(vendor["source_path"]), _PreviewColumns(), source_id=vendor["source_id"])
    vendor["rows_summary"] = {s: len(r) for s, r in built["rows"].items()}
    vendor["warnings"] = built["warnings"]
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.post("/ui/consolidation/from-run/{run_id}")
async def consolidation_from_run(request: Request, run_id: str):
    """Task 1 entry: a validated document run enters a fresh consolidation
    case. With a request form attached to the run (and zero doc↔form
    mismatches — export_gate enforces it) the FORM becomes the full vendor;
    a lone document becomes a kind="doc" fragment."""
    form = await request.form()
    confirm = form.get("confirm") == "on"
    ok, reason = docrows.export_gate(run_id, confirm)
    if not ok:
        raise HTTPException(409, detail=reason)

    case = casestore.new_case()
    case_id = case["case_id"]
    vkey = casestore.next_vkey(case)
    meta = runstore.load(run_id, "meta.json") or {}

    form_path = docrows.attached_form_path(run_id)
    if form_path is not None and convert.looks_like_vendor_form(form_path):
        extract = convert.extract_form(form_path)
        built = convert.build_vendor_rows(form_path, _PreviewColumns())
        sid = built["partner_id"] or sidmod.generate(set())
        built = convert.build_vendor_rows(form_path, _PreviewColumns(),
                                          source_id=sid)
        vendor = {
            "vkey": vkey, "kind": "form", "label": form_path.name,
            "source_path": str(form_path),
            "attached_run_id": run_id, "merge": {"choices": {}},
            "source_id": sid,
            "status": "error" if built["errors"] else "extracted",
            "errors": built["errors"],
            "warnings": built["warnings"],
            "profile": built["profile"].to_dict(),
            "rows_summary": {s: len(r) for s, r in built["rows"].items()},
            "added": datetime.now().isoformat(timespec="seconds"),
        }
        if not built["errors"]:
            # export_gate already proved 0 doc-vs-form mismatch, so the merge
            # finds no ambiguous field (no quiz) — the document just makes the
            # bank values authoritative, consistent with the guided flow.
            _recompute_merge(vendor)
        coverage = convert.coverage(extract["fields"], built["rows"],
                                    built["unmapped"])
        casestore.save_extract(case_id, vkey, {
            "extract": convert.masked_extract(extract), "coverage": coverage})
    else:
        sid = sidmod.generate(set())
        built = docrows.rows_from_run(run_id, _PreviewColumns(), source_id=sid)
        vendor = {
            "vkey": vkey, "kind": "doc",
            "label": meta.get("file_name", run_id),
            "run_id": run_id, "source_path": "",
            "source_id": sid,
            "status": "error" if built["errors"] else "extracted",
            "errors": built["errors"],
            "warnings": built["warnings"],
            "profile": {"form_family": "document",
                        "vendor_country": "", "bank_country": ""},
            "rows_summary": {s: len(r) for s, r in built["rows"].items()},
            "added": datetime.now().isoformat(timespec="seconds"),
        }
        casestore.save_extract(case_id, vkey, {
            "extract": {}, "coverage": docrows.coverage_for_run(run_id, built)})

    case["vendors"].append(vendor)
    casestore.save_case(case)
    return RedirectResponse(f"/ui/consolidation/{case_id}", status_code=303)


@router_consol.get("/ui/consolidation/{case_id}/download")
def case_download(case_id: str):
    case = _load_case_or_404(case_id)
    rec = casestore.latest_verified_output(case)
    if rec is None:
        raise HTTPException(409, detail="no verified output — consolidate first")
    p = casestore.output_path(case_id, rec["name"])
    if p is None or not p.exists():
        raise HTTPException(404)
    stem = Path(case["template"]["orig_name"]).stem if case.get("template") else "BusinessPartnerTemplate"
    return FileResponse(
        p, filename=f"{stem}_consolidated.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
