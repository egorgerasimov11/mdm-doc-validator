#!/usr/bin/env python3
"""
api.py — REST surface (/api/v1). Two routers:
  router_core  — check / runs / jobs / doctor / rules (ships to BTP)
  router_teach — review, labels, training, eval (operator-only, excluded in
                 api-only mode so the BTP OpenAPI is honest)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import PlainTextResponse

from .. import config, dataset, model_client as mc, review_core, runstore
from ..pipeline import UnreadableDocument, run_check
from . import jobs
from .deps import api_error, require_token, save_upload
from .schemas import EvalIn, FeedbackIn, FewshotIn, LoraIn, ModelfileIn, ReviewSubmission

router_core = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])
router_teach = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)],
                         tags=["teach"])

ARTIFACT_ALLOWLIST = {"bulk_report.json", "bulk_report.md",
                      "meta.json", "stage_a.json", "extraction.json",
                      "findings.json", "report.json", "report.md", "sap_compare.json",
                      "web_evidence.json", "reasoning.md", "template_compare.json"}


def _labeled_ids() -> set[str]:
    return {l.get("doc_sha256") for l in dataset.load_labels()}


# ---------------------------------------------------------------- system ------
@router_core.get("/doctor", tags=["system"])
def doctor() -> dict:
    out: dict = {"mode": config_mode(), "project_root": str(config.PROJECT_ROOT)}
    try:
        models = sorted(mc.preflight())
        out["model_host"] = {"url": mc.host(), "source": mc.host_source(), "reachable": True}
        out["models"] = models
        out["roles"] = {}
        for role, configured in mc.ROLES.items():
            resolved = mc.resolve(role)
            out["roles"][role] = {"configured": configured, "resolved": resolved,
                                  "present": resolved in models or f"{resolved}:latest" in models}
    except mc.OllamaUnavailable as e:
        mc.reset_host()  # so the next probe retries the tunnel
        out["model_host"] = {"url": None, "source": "", "reachable": False, "error": str(e)}
        out["models"], out["roles"] = [], {}
    tess = shutil.which("tesseract")
    langs: list = []
    if tess:
        try:
            raw = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                                 timeout=10).stdout.decode()
            langs = [l for l in raw.split()[1:] if "/" not in l and l.isalpha()]
        except Exception:
            pass
    out["tesseract"] = {"path": tess, "langs": sorted(langs)}
    out["dirs"] = {name: {"path": str(p), "exists": p.exists()} for name, p in (
        ("rules", config.RULES_DIR), ("prompts", config.PROMPTS_DIR),
        ("templates", config.TEMPLATES_DIR), ("runs", config.RUNS_DIR),
        ("dataset", config.DATASET_DIR), ("inbox", config.INBOX_DIR))}
    out["labels_count"] = dataset.count_labels()
    out["runs_count"] = len(runstore.list_runs())
    return out


def config_mode() -> str:
    import os
    return os.environ.get("MDMDOC_MODE", "full")


def _rules_path(doc_class: str) -> Path:
    from .. import rules_io
    return rules_io.rules_path(doc_class)


@router_core.get("/rules", tags=["system"])
def get_rules(doc_class: str = "bank") -> dict:
    import yaml
    p = _rules_path(doc_class)
    if not p.exists():
        raise api_error(404, "not_found", f"rules file for {doc_class} not found")
    return yaml.safe_load(p.read_text()) or {}


@router_core.get("/rules/raw", response_class=PlainTextResponse, tags=["system"])
def get_rules_raw(doc_class: str = "bank") -> str:
    p = _rules_path(doc_class)
    if not p.exists():
        raise api_error(404, "not_found", f"rules file for {doc_class} not found")
    return p.read_text()


# ---- rule authoring (operator-only): edit / delete / regenerate for SAP ------
@router_teach.post("/rules/{doc_class}/raw", tags=["rules"])
def save_rules_raw(doc_class: str, payload: dict) -> dict:
    """Overwrite a doc-class rule file with edited YAML (delete = remove a block).
    The model never decides verdicts — rules stay explicit and editable. The write
    is done in rules_io (the named rule-write choke point)."""
    from .. import rules_io
    try:
        n = rules_io.save_rules(doc_class, payload.get("yaml", ""))
    except ValueError as e:
        raise api_error(400, "bad_yaml", str(e))
    return {"ok": True, "doc_class": doc_class, "rules": n}


@router_teach.post("/rules/regenerate", tags=["rules"])
def regenerate_rules() -> dict:
    from .. import rules_io
    return rules_io.regenerate_abap()


@router_teach.post("/rules/{doc_class}/approve", tags=["rules"])
def approve_rule(doc_class: str, body: dict) -> dict:
    """HARD GATE: record the human's decision on a rule — decision ∈
    approved|rejected|pending. `rule_id` for one, or `all_pending: true` to
    approve every currently-pending rule at once. Only approved rules ever fire."""
    import yaml

    from .. import rule_approvals
    if doc_class not in ("bank", "w9"):
        raise api_error(400, "bad_request", "doc_class must be 'bank' or 'w9'")
    decision = body.get("decision", "approved")
    if decision not in ("approved", "rejected", "pending"):
        raise api_error(400, "bad_request", "decision must be approved/rejected/pending")
    cfg = yaml.safe_load(_rules_path(doc_class).read_text() or "") or {}
    by_id = {str(r.get("id")): r for r in cfg.get("rules", []) if isinstance(r, dict)}
    if body.get("all_pending"):
        store = rule_approvals.load()
        ids = [rid for rid, r in by_id.items()
               if rule_approvals.status(store, doc_class, r) == rule_approvals.PENDING]
    else:
        ids = body.get("rule_ids") or ([body["rule_id"]] if body.get("rule_id") else [])
    n = 0
    for rid in ids:
        r = by_id.get(str(rid))
        if r:
            rule_approvals.set_decision(doc_class, r, decision, note=body.get("note", ""))
            n += 1
    return {"ok": True, "updated": n, "decision": decision}


@router_teach.get("/rules/stats", tags=["rules"])
def rules_stats() -> dict:
    """П7: per-rule firing stats + tier promotion/demotion PROPOSALS from the
    local run history and confirmed labels. Read-only; application happens via
    the explicit tier endpoint below (a human decision)."""
    from .. import rule_stats
    return rule_stats.build()


@router_teach.post("/rules/{doc_class}/tier", tags=["rules"])
def set_rule_tier(doc_class: str, body: dict) -> dict:
    """Apply ONE tier change (the human's approval of a proposal). Surgical
    tier-line edit; tier is approval-hash-immune so the rule does NOT reset to
    pending — the hard gate is untouched."""
    from .. import rules_io
    if doc_class not in ("bank", "w9"):
        raise api_error(400, "bad_request", "doc_class must be 'bank' or 'w9'")
    rid = str(body.get("rule_id") or "")
    tier = str(body.get("tier") or "")
    if not rid:
        raise api_error(400, "bad_request", "rule_id required")
    try:
        return {"ok": True, **rules_io.set_rule_tier(doc_class, rid, tier)}
    except ValueError as e:
        raise api_error(400, "bad_request", str(e))


# ---------------------------------------------------------------- settings ----
@router_teach.get("/settings", tags=["system"])
def get_settings() -> dict:
    """Operator runtime settings. `engine` is the default analysis engine for
    checks that do not pass one; MDMDOC_ENGINE env (ops) overrides the panel."""
    env = os.environ.get("MDMDOC_ENGINE", "").strip().lower()
    return {"engine": config.engine_mode(),
            "engine_saved": str(config.load_settings().get("engine", "")) or "auto",
            "engine_env_override": env if env in config.ENGINE_MODES else "",
            "engine_modes": list(config.ENGINE_MODES),
            "default_effort": config.default_effort()}


@router_teach.post("/settings", tags=["system"])
def set_settings(body: dict) -> dict:
    out = {"ok": True}
    if "engine" in body:
        eng = str(body.get("engine", "")).strip().lower()
        if eng not in config.ENGINE_MODES:
            raise api_error(400, "bad_request",
                            f"engine must be one of {', '.join(config.ENGINE_MODES)}")
        config.save_setting("engine", eng)
    if "default_effort" in body:
        try:
            lvl = int(body["default_effort"])
        except (TypeError, ValueError):
            raise api_error(400, "bad_request", "default_effort must be 1..5")
        if lvl not in (1, 2, 3, 4, 5):
            raise api_error(400, "bad_request", "default_effort must be 1..5")
        config.save_setting("default_effort", lvl)
    out.update({"engine": config.engine_mode(),
                "default_effort": config.default_effort(),
                "engine_env_override": os.environ.get("MDMDOC_ENGINE", "").strip().lower()})
    return out


# ---------------------------------------------------------------- check -------
def _run_pipeline(path: Path, doc_class: str, lang: str, use_vision: bool,
                  sap_image: Path | None = None, quality: bool = False,
                  web: bool = False, engine: str = "", sap_bp: str = "",
                  job=None, effort: int = 0,
                  template_path: Path | None = None) -> dict:
    mc.reset_host()
    # HARD GATE default ON (Egor's choice); MDMDOC_RULE_GATE=0 is the instant
    # off-switch (no redeploy) if the "everything held for approval" phase is
    # disruptive — set it in the plist env and unload/load.
    gate = os.environ.get("MDMDOC_RULE_GATE", "1").strip().lower() in ("1", "true", "on", "yes")

    def on_stage(stage: str, pct: int) -> None:
        if job is not None:
            job.stage, job.percent = stage, pct

    # FIFO gate over the single pipeline slot (D2): fair ordering for queued
    # checks, cancel honored while waiting; legacy `with jobs.PIPELINE_LOCK`
    # users keep strict mutual exclusion (the gate holds the same lock)
    with jobs.GATE.slot(job):
        res = run_check(path, doc_class, use_vision=use_vision, lang=lang,
                        sap_image=sap_image, quality=quality,
                        web_evidence=True if web else None, enforce_approvals=gate,
                        engine=engine or None, sap_bp=sap_bp,
                        cancel=(job.cancel if job is not None else None),
                        on_stage=on_stage, effort=effort or None,
                        template_path=template_path)
    report = json.loads(res.report_json)
    return {"run_id": res.run_id, "verdict": res.verdict, "report": report,
            "report_md": res.report_md}


@router_core.post("/check", tags=["check"])
def check(file: UploadFile | None = File(None), doc_class: str = Form("auto"),
          lang: str = Form("en"), use_vision: bool = Form(True),
          wait: bool = Form(True), sap_file: UploadFile | None = File(None),
          rerun_run_id: str = Form(""), quality: bool = Form(False),
          web: bool = Form(False), engine: str = Form(""), sap_bp: str = Form(""),
          effort: int = Form(0), template_file: UploadFile | None = File(None)):
    if doc_class not in ("bank", "w9", "auto"):
        raise api_error(400, "bad_request", "doc_class must be 'bank', 'w9' or 'auto'")
    engine = engine.strip().lower()
    if engine and engine not in config.ENGINE_MODES:
        raise api_error(400, "bad_request",
                        f"engine must be one of {', '.join(config.ENGINE_MODES)} (or empty for the default)")
    if web and os.environ.get("MDMDOC_MODE", "").strip() == "api-only":
        # the sealed BTP image promises NO outbound calls — the operator-console
        # click-opt-in does not exist there, so web=true must not slip through
        raise api_error(400, "bad_request",
                        "external web evidence is disabled in the api-only deployment")
    if lang not in ("en", "ru"):
        raise api_error(400, "bad_request", "lang must be 'en' or 'ru'")
    if effort and effort not in (1, 2, 3, 4, 5):
        raise api_error(400, "bad_request", "effort must be 1..5 (or 0 for legacy params)")
    if file is not None:
        path = save_upload(file.filename or "document", file.file.read())
    elif rerun_run_id:
        # compare-after-the-fact: re-run a stored document (full values only exist
        # in memory during a run, so a fresh SAP comparison means a fresh run)
        rid = runstore.resolve_run(rerun_run_id)
        meta = runstore.load(rid, "meta.json") if rid else None
        if not meta or not Path(meta.get("path", "")).exists():
            raise api_error(404, "not_found", f"run {rerun_run_id} has no re-runnable document")
        path = Path(meta["path"])
    else:
        raise api_error(400, "bad_request", "provide a file or rerun_run_id")
    sap_path = None
    if sap_file is not None:
        # a table export (.xlsx BUT0BK/BUT000) works for BOTH bank and w9; a
        # screenshot image is bank-only (the vision reader targets the Bank
        # Details screen). doc_class=auto is resolved inside the pipeline.
        sap_name = sap_file.filename or "screen.png"
        is_table = Path(sap_name).suffix.lower() in (".xlsx", ".xlsm")
        if doc_class == "w9" and not is_table:
            raise api_error(400, "bad_request",
                            "for a W-9 attach a BUT000 table export (.xlsx); "
                            "SAP screenshots apply to bank documents")
        sap_path = save_upload("sap__" + sap_name, sap_file.file.read())
    tpl_path = None
    if template_file is not None:
        tpl_name = template_file.filename or "template.xlsx"
        if Path(tpl_name).suffix.lower() not in (".xlsx", ".xlsm"):
            raise api_error(400, "bad_request",
                            "the template must be a request-form workbook (.xlsx/.xlsm)")
        tpl_path = save_upload("tpl__" + tpl_name, template_file.file.read())
    from ..estimate import estimate_seconds, human, sniff_text_layer
    est = estimate_seconds("bank" if doc_class == "auto" else doc_class,
                           sniff_text_layer(path), use_vision=use_vision,
                           sap=sap_path is not None or tpl_path is not None,
                           quality=quality, effort=effort or None)
    if wait:
        try:
            out = _run_pipeline(path, doc_class, lang, use_vision, sap_path,
                                quality, web, engine, sap_bp, effort=effort,
                                template_path=tpl_path)
            out["estimate_s"] = est
            return out
        except UnreadableDocument as e:
            raise api_error(422, "unreadable_document", str(e))
        except mc.OllamaUnavailable as e:
            raise api_error(503, "model_host_down", str(e))

    def work(log, job):
        job.estimate_s = est
        job.label = path.name
        log(f"estimated duration: {human(est)}")
        log(f"document: {path.name}")
        if sap_path:
            log(f"SAP screenshot: {sap_path.name}")
        log(f"running {doc_class} pipeline{' (thorough tier)' if quality else ''}"
            f"{' + external web evidence' if web else ''}…")
        if tpl_path:
            log(f"template: {tpl_path.name}")
        out = _run_pipeline(path, doc_class, lang, use_vision, sap_path, quality,
                            web, engine, sap_bp, job=job, effort=effort,
                            template_path=tpl_path)
        out["estimate_s"] = est
        log(f"verdict: {out['verdict']} (run {out['run_id']})")
        return out

    job = jobs.REGISTRY.submit("check", work, pass_job=True)
    from fastapi.responses import JSONResponse
    return JSONResponse({"job_id": job.id, "estimate_s": est}, status_code=202)


@router_core.post("/check-routing", tags=["check"])
def check_routing(routing: str = Form(""), account: str = Form(""),
                  bank_name: str = Form(""), web: bool = Form(False),
                  lang: str = Form("en")) -> dict:
    """US bank-keys quick check — a TYPED routing/account pair, no document.
    Deciders are the same as a document run: the approval-gated YAML rules
    (BNK-040..046 routing/account arithmetic). Live directory evidence is
    opt-in and NOTE-only (invariant #1: web never moves the verdict) — a
    directory miss surfaces as web_hint, not as a REJECT. No OCR and no model:
    the input is already structured, so this answers deterministically."""
    routing, account, bank_name = routing.strip(), account.strip(), bank_name.strip()
    if not routing and not account:
        raise api_error(400, "bad_request",
                        "provide a routing number and/or an account number")
    if lang not in ("en", "ru"):
        raise api_error(400, "bad_request", "lang must be 'en' or 'ru'")
    if web and os.environ.get("MDMDOC_MODE", "").strip() == "api-only":
        raise api_error(400, "bad_request",
                        "external web evidence is disabled in the api-only deployment")
    from ..fields import Extraction
    from ..rules.engine import run_rules
    from ..verdict import decide, next_step
    fields: dict = {"bank_country": "US"}
    if routing:
        fields["routing_aba"] = routing
    if account:
        fields["account_number"] = account
    if bank_name:
        fields["bank_name"] = bank_name
    # doc_type stays EMPTY: there is no document, so document-typed rules
    # (applies_to: invoice/payment_instructions/…) must not fire — only the
    # field-arithmetic rules (routing/account/SWIFT/IBAN shape) apply.
    ext = Extraction(doc_class="bank", doc_type="", fields=fields)
    ext.register_secrets()
    gate = os.environ.get("MDMDOC_RULE_GATE", "1").strip().lower() in ("1", "true", "on", "yes")
    findings = run_rules(ext, lang=lang, enforce_approvals=gate)
    web_rows: list[dict] = []
    web_hint = ""
    if web:
        from .. import web_enrichment
        web_findings, web_rows = web_enrichment.gather(ext, force=True)
        findings.extend(web_findings)
        for row in web_rows:
            if row.get("check") == "aba_directory" and row.get("status") == "not_found":
                web_hint = ("routing number not found in any live directory — "
                            "unassigned or retired; verify with the bank before payment")
    verdict = decide(findings)
    return {"verdict": verdict, "next_step": next_step("bank", verdict),
            "findings": [f.to_dict() for f in findings],
            "web": web_rows, "web_hint": web_hint}


# ---------------------------------------------------------------- bulk --------
@router_teach.post("/bulk", tags=["bulk"])
def bulk(file: UploadFile = File(...), case: str = Form("auto"),
         web: bool = Form(False),
         refs: list[UploadFile] = File(default=[])):
    """MASS table validation (V-wave): a canonical template or a raw SE16N
    export -> per-row buckets + a full-value result workbook in inbox/ and
    masked bulk_report artifacts under runs/. Always async (202 + job)."""
    if case not in ("auto", "bank", "tax", "region"):
        raise api_error(400, "bad_request",
                        "case must be auto, bank, tax or region")
    name = file.filename or "table.xlsx"
    if Path(name).suffix.lower() not in (".xlsx", ".xlsm"):
        raise api_error(400, "bad_request", "bulk input must be .xlsx/.xlsm")
    path = save_upload("bulk__" + name, file.file.read())
    ref_paths = [save_upload("bulkref__" + (r.filename or "ref.xlsx"),
                             r.file.read()) for r in refs or []]

    def work(log, job):
        job.label = name
        from ..bulk import run_bulk
        from ..bulk.reader import BulkInputError
        try:
            with jobs.GATE.slot(job):
                res, arts = run_bulk(path, case=case, refs=ref_paths, web=web,
                                     progress=log)
        except BulkInputError as e:
            raise RuntimeError(str(e)) from e
        out = {"bulk_id": arts["bulk_id"], "case": res.case,
               "summary": res.summary(),
               "result_name": Path(arts["result_xlsx"]).name}
        log(f"buckets: {res.counts()}")
        return out

    job = jobs.REGISTRY.submit("bulk", work, pass_job=True)
    from fastapi.responses import JSONResponse
    return JSONResponse({"job_id": job.id}, status_code=202)


@router_teach.get("/bulk/templates/{case}", tags=["bulk"])
def bulk_template(case: str):
    from fastapi.responses import FileResponse
    if case not in ("bank", "tax", "region"):
        raise api_error(404, "not_found", "unknown template")
    fname = {"bank": "banking_template.xlsx", "tax": "tax_template.xlsx",
             "region": "postal_region_template.xlsx"}[case]
    p = config.TEMPLATES_DIR / "bulk" / fname
    if not p.exists():
        raise api_error(404, "not_found",
                        "template not generated — run tools/gen_bulk_templates.py")
    return FileResponse(p, filename=fname, headers={"Cache-Control": "no-store"})


@router_teach.get("/bulk/{bulk_id}/result", tags=["bulk"])
def bulk_result(bulk_id: str):
    """The FULL-VALUE validated workbook from inbox/ (operator's own data)."""
    from fastapi.responses import FileResponse
    rid = "".join(c for c in bulk_id if c in "0123456789abcdef")
    hits = sorted(config.INBOX_DIR.glob(f"{rid}__*_validated.xlsx"))
    if not hits:
        raise api_error(404, "not_found", f"no bulk result for {bulk_id}")
    return FileResponse(hits[-1], filename=hits[-1].name.split("__", 1)[-1],
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- runs --------
@router_core.get("/runs", tags=["runs"])
def runs(limit: int = 50, doc_class: str | None = None) -> list[dict]:
    labeled = _labeled_ids()
    rows = runstore.list_runs()
    if doc_class:
        rows = [r for r in rows if r.get("doc_class") == doc_class]
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    for r in rows:
        r["labeled"] = r["run_id"] in labeled
    return rows[:limit]


@router_core.get("/runs/{run_id}", tags=["runs"])
def run_detail(run_id: str) -> dict:
    rid = runstore.resolve_run(run_id)
    if not rid:
        raise api_error(404, "not_found", f"run {run_id} not found")
    rep = runstore.load(rid, "report.json")
    if isinstance(rep, str):
        rep = json.loads(rep)
    return {"run_id": rid,
            "meta": runstore.load(rid, "meta.json") or {},
            "extraction": runstore.load(rid, "extraction.json") or {},
            "findings": runstore.load(rid, "findings.json") or [],
            "report": rep or {},
            "report_md": runstore.load(rid, "report.md") or "",
            "labeled": rid in _labeled_ids()}


@router_core.get("/runs/{run_id}/artifacts/{name}", tags=["runs"])
def artifact(run_id: str, name: str):
    if name not in ARTIFACT_ALLOWLIST:
        raise api_error(404, "not_found", f"unknown artifact {name!r}")
    rid = runstore.resolve_run(run_id)
    if not rid:
        raise api_error(404, "not_found", f"run {run_id} not found")
    data = runstore.load(rid, name)
    if data is None:
        raise api_error(404, "not_found", f"{name} missing for run {rid}")
    if name.endswith(".md") or isinstance(data, str):
        return PlainTextResponse(str(data), media_type="text/markdown")
    return data


# ---------------------------------------------------------------- jobs --------
@router_core.get("/jobs", tags=["jobs"])
def jobs_list() -> list[dict]:
    return [j.to_dict() for j in jobs.REGISTRY.list()]


@router_core.get("/jobs/{job_id}", tags=["jobs"])
def job_detail(job_id: str, after: int = 0) -> dict:
    j = jobs.REGISTRY.get(job_id)
    if not j:
        raise api_error(404, "not_found", f"job {job_id} not found")
    return j.to_dict(after=after)


@router_core.post("/jobs/{job_id}/cancel", tags=["jobs"])
def job_cancel(job_id: str) -> dict:
    """Cooperative cancel (D2): a queued check dies at the gate without ever
    running; a running one stops at its next pipeline checkpoint (an in-flight
    model call finishes first). Only check jobs are cancelable."""
    j = jobs.REGISTRY.get(job_id)
    if not j:
        raise api_error(404, "not_found", f"job {job_id} not found")
    if j.kind not in ("check", "bulk"):
        raise api_error(409, "conflict", f"jobs of kind '{j.kind}' are not cancelable")
    if j.status in ("done", "error", "canceled"):
        raise api_error(409, "conflict", f"job already {j.status}")
    jobs.REGISTRY.cancel(job_id)
    return {"ok": True, "status": j.status,
            "note": "stopping after the current model call finishes"}


# ================================================================= teach =======
@router_teach.get("/runs/{run_id}/document")
def download_document(run_id: str, src: str = "doc"):
    """Stream the ORIGINAL analysed document back to the operator (D5). Lives
    on the teach router for the same reason as /preview: the file holds full
    sensitive values. src=container returns the uploaded envelope (zip/eml/
    xlsm) when the run analysed a packet member."""
    from fastapi.responses import FileResponse
    rid = runstore.resolve_run(run_id)
    meta = runstore.load(rid, "meta.json") if rid else None
    if not meta:
        raise api_error(404, "not_found", f"run {run_id} not found")
    p = Path(meta.get("path", ""))
    if src == "container":
        hits = sorted(config.INBOX_DIR.glob(f"{rid}__*")) if config.INBOX_DIR.exists() else []
        files = [h for h in hits if h.is_file()]
        if files:
            p = files[0]
    if not p.exists() or not p.is_file():
        raise api_error(404, "not_found",
                        "original document is no longer on disk (inbox cleaned?)")
    return FileResponse(p, filename=meta.get("file_name") or p.name,
                        headers={"Cache-Control": "no-store"})


@router_teach.get("/runs/{run_id}/preview/{page}")
def preview_page(run_id: str, page: int, src: str = "doc"):
    """On-demand page render of the ORIGINAL document (or the SAP screenshot).
    Streamed to the operator, never persisted — pixels hold full sensitive data,
    which is exactly why this lives on the teach router (absent in BTP)."""
    from fastapi.responses import Response
    rid = runstore.resolve_run(run_id)
    meta = runstore.load(rid, "meta.json") if rid else None
    if not meta:
        raise api_error(404, "not_found", f"run {run_id} not found")
    key = "sap_path" if src == "sap" else "path"
    p = Path(meta.get(key) or "")
    if not p.exists():
        raise api_error(404, "not_found", f"{src} file for run {rid} is gone")
    try:
        if p.suffix.lower() == ".pdf":
            import fitz
            doc = fitz.open(p)
            if page < 0 or page >= doc.page_count:
                doc.close()
                raise api_error(404, "not_found", f"page {page} out of range")
            png = doc[page].get_pixmap(dpi=120).tobytes("png")
            doc.close()
            return Response(content=png, media_type="image/png",
                            headers={"Cache-Control": "no-store"})
        data = p.read_bytes()
        media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return Response(content=data, media_type=media,
                        headers={"Cache-Control": "no-store"})
    except Exception as e:  # noqa: BLE001
        raise api_error(422, "unreadable_document", f"cannot render preview: {e}")


@router_teach.get("/runs/{run_id}/evidence/{key}")
def evidence_crop(run_id: str, key: str):
    """On-demand crop of the zone that evidences a finding/field (W-9 checkbox
    row, TIN boxes, signature area, bank account/routing line). Rendered into a
    temp dir and streamed like the preview above — full sensitive pixels, never
    persisted, teach router only (absent in BTP)."""
    from fastapi.responses import Response

    from ..evidence import EVIDENCE_KEYS, render_crop
    if key not in EVIDENCE_KEYS:
        raise api_error(404, "not_found", f"unknown evidence key {key!r}")
    rid = runstore.resolve_run(run_id)
    if not rid:
        raise api_error(404, "not_found", f"run {run_id} not found")
    try:
        png = render_crop(rid, key)
    except Exception as e:  # noqa: BLE001
        raise api_error(422, "unreadable_document", f"cannot render evidence: {e}")
    if png is None:
        raise api_error(404, "not_found", f"no {key} evidence zone for run {rid}")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router_teach.get("/runs/{run_id}/review")
def review_form(run_id: str) -> dict:
    try:
        return review_core.review_defaults(run_id)
    except review_core.RunNotFound:
        raise api_error(404, "not_found", f"run {run_id} not found")


@router_teach.post("/runs/{run_id}/label")
def submit_label(run_id: str, sub: ReviewSubmission) -> dict:
    """Save the correction AND retrain immediately (unless retrain=false):
    few-shot rebuild -> custom model rebuild on the model host -> re-run the
    document so the corrected verdict is visible right away (precedent applies)."""
    try:
        result = review_core.submit_review(run_id, sub.model_dump())
    except review_core.RunNotFound:
        raise api_error(404, "not_found", f"run {run_id} not found")
    except ValueError as e:   # leak gate — never echo details beyond scrubbed text
        raise api_error(400, "bad_request", str(e))
    # note-to-rule channel (D11b): a substantial note (or error_source=rule_wrong)
    # is classified async; a clean ADDITIVE rule proposal lands as PENDING
    note = (sub.notes or "").strip()
    if len(note) >= 12 or (sub.error_source == "rule_wrong" and note):
        from .. import learning

        def note_work(log, _note=note, _rid=run_id):
            mc.reset_host()
            with jobs.PIPELINE_LOCK:
                return learning.note_to_rule(_rid, _note, log=log)

        nj = jobs.REGISTRY.submit("note-rule", note_work)
        result["note_rule_job_id"] = nj.id
    if not sub.retrain:
        return result

    rid = runstore.resolve_run(run_id)
    meta = runstore.load(rid, "meta.json") or {}

    def work(log):
        from ..adoption import build_candidate
        from ..fewshot import build_fewshot
        log("1/3 rebuilding few-shot exemplars from your corrections…")
        build_fewshot(k=2)
        log("2/3 building the CANDIDATE model on the model host (production "
            "mdmdoc-extract is untouched — adopt it from Training after the gated eval)…")
        try:
            mc.reset_host()
            build_candidate(progress=log)
        except Exception as e:  # noqa: BLE001 — training must not block the precedent
            log(f"    candidate build skipped ({e.__class__.__name__}) — few-shot still applied")
        p = Path(meta.get("path", ""))
        if p.exists():
            log("3/3 re-running the document with the corrections applied…")
            out = _run_pipeline(p, meta.get("doc_class", "bank"), "en", True)
            log(f"new verdict: {out['verdict']} (was corrected by your precedent)")
            return {**result, "rerun": {"run_id": out["run_id"], "verdict": out["verdict"]}}
        log("3/3 original file missing — skipping re-run (precedent will apply next time)")
        return result

    job = jobs.REGISTRY.submit("retrain", work, capture_stdout=True)
    return {**result, "retrain_job_id": job.id}


@router_teach.post("/runs/{run_id}/mark-valid", tags=["teach"])
def mark_valid(run_id: str) -> dict:
    """One-click 'this document is VALID' (D11c): a confirmed ACCEPT label —
    the precedent makes it stick for this document (C11 relax gate satisfied
    by verdict_confirmed) and the pattern memory learns the shape for others.
    No model call, no retrain."""
    sub = ReviewSubmission(verdict_gold="ACCEPT", verdict_confirmed=True,
                           retrain=False, notes="", scenarios=[])
    try:
        result = review_core.submit_review(run_id, sub.model_dump())
    except review_core.RunNotFound:
        raise api_error(404, "not_found", f"run {run_id} not found")
    except ValueError as e:
        raise api_error(400, "bad_request", str(e))
    return {**result, "marked_valid": True}


@router_teach.post("/runs/{run_id}/rating", tags=["teach"])
def rate_run(run_id: str, body: dict) -> dict:
    """Thumbs up/down (D11e): a one-tap operator quality signal. Down-votes
    boost the run in the training queue and flag it on the page; up-votes are
    a lightweight confirmation. Structure only — never values, never a model."""
    rating = str(body.get("rating", "")).strip().lower()
    if rating not in ("up", "down"):
        raise api_error(400, "bad_request", "rating must be 'up' or 'down'")
    rid = runstore.resolve_run(run_id)
    if not rid or not runstore.load(rid, "meta.json"):
        raise api_error(404, "not_found", f"run {run_id} not found")
    from .. import ratings
    row = ratings.record(rid, rating)
    return {"ok": True, **row}


@router_teach.post("/runs/{run_id}/propose-fix", tags=["teach"])
def propose_fix(run_id: str, body: FeedbackIn) -> dict:
    """Free-text operator feedback ('this rule fired wrongly…') -> the strong
    model classifies it and, if it is a rule problem, PROPOSES a rule-file edit
    (diff) for review. Proposal only; applying goes through the rules editor's
    save (rules_io.save_rules). Runs as a job — the strong model is slow."""
    if not runstore.resolve_run(run_id):
        raise api_error(404, "not_found", f"run {run_id} not found")
    fb = (body.feedback or "").strip()
    if not fb:
        raise api_error(400, "bad_request", "feedback is empty")

    def work(log):
        from .. import rule_propose
        log("analysing your feedback against the rules that fired…")
        mc.reset_host()
        with jobs.PIPELINE_LOCK:   # the strong model must not race the pipeline
            result = rule_propose.propose(run_id, fb, (body.rule_id or "").strip())
        log(f"proposal ready — {result.get('kind')}")
        return result

    return {"job_id": jobs.REGISTRY.submit("propose", work).id}


@router_teach.get("/labels")
def labels() -> dict:
    labs = dataset.load_labels()
    return {"count": len(labs), "labels": labs}


@router_teach.post("/train/fewshot")
def train_fewshot(body: FewshotIn) -> dict:
    from ..fewshot import build_fewshot
    lines: list[str] = []
    router = jobs.install_stdout_router()
    router.set_sink(lambda s: lines.extend(l for l in s.splitlines() if l.strip()))
    try:
        rc = build_fewshot(k=body.k)
    finally:
        router.clear_sink()
    return {"rc": rc, "log": lines}


@router_teach.post("/train/modelfile")
def train_modelfile(body: ModelfileIn):
    from ..modelfile import build_modelfile

    def work(log):
        log(f"building Modelfile (apply={body.apply})…")
        rc = build_modelfile(apply=body.apply)
        log(f"done rc={rc}")
        return {"rc": rc}

    job = jobs.REGISTRY.submit("modelfile", work, capture_stdout=True)
    from fastapi.responses import JSONResponse
    return JSONResponse({"job_id": job.id}, status_code=202)


@router_teach.get("/train/adoption")
def adoption_state() -> dict:
    from .. import adoption
    return adoption.load_state()


@router_teach.post("/train/candidate")
def train_candidate():
    """Build the candidate model and run the gated eval against it."""
    if jobs.REGISTRY.running({"eval", "check", "adoption"}):
        raise api_error(409, "job_conflict", "an eval/check/adoption job is already running")
    from .. import adoption

    def work(log):
        mc.reset_host()
        with jobs.PIPELINE_LOCK:
            state = adoption.build_and_eval_candidate(progress=log)
        return {"candidate": state.get("candidate")}

    job = jobs.REGISTRY.submit("adoption", work, capture_stdout=True)
    from fastapi.responses import JSONResponse
    return JSONResponse({"job_id": job.id}, status_code=202)


@router_teach.post("/train/adopt")
def train_adopt() -> dict:
    from .. import adoption
    try:
        state = adoption.adopt()
    except RuntimeError as e:
        raise api_error(409, "gate_failed", str(e))
    return {"adopted": state.get("adopted")}


@router_teach.post("/train/rollback")
def train_rollback() -> dict:
    from .. import adoption
    try:
        state = adoption.rollback()
    except RuntimeError as e:
        raise api_error(409, "rollback_failed", str(e))
    return {"adopted": state.get("adopted")}


@router_teach.post("/train/export-lora")
def train_export_lora(body: LoraIn) -> dict:
    from ..lora_export import export_lora
    lines: list[str] = []
    router = jobs.install_stdout_router()
    router.set_sink(lambda s: lines.extend(l for l in s.splitlines() if l.strip()))
    try:
        rc = export_lora(min_labels=body.min_labels, force=body.force, split=body.split)
    finally:
        router.clear_sink()
    return {"rc": rc, "log": lines}


@router_teach.post("/eval")
def eval_start(body: EvalIn):
    if jobs.REGISTRY.running({"eval", "check"}):
        raise api_error(409, "job_conflict", "an eval or check job is already running")
    from ..evalrun import run_eval

    def work(log):
        mc.reset_host()
        with jobs.PIPELINE_LOCK:
            rc = run_eval(only=body.only, limit=body.limit, tag=body.tag,
                          scenario=body.scenario, progress=log)
        hist = eval_history()
        return {"rc": rc, "metrics": (hist[-1]["metrics"] if hist else None)}

    job = jobs.REGISTRY.submit("eval", work)
    from fastapi.responses import JSONResponse
    return JSONResponse({"job_id": job.id}, status_code=202)


@router_teach.get("/eval/history")
def eval_history() -> list[dict]:
    p = config.EVAL_DIR / "history.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


@router_teach.get("/eval/report")
def eval_report():
    p = config.EVAL_DIR / "report.md"
    if not p.exists():
        raise api_error(404, "not_found", "no eval report yet")
    return PlainTextResponse(p.read_text(), media_type="text/markdown")
