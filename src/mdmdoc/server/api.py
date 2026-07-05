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
from .schemas import EvalIn, FewshotIn, LoraIn, ModelfileIn, ReviewSubmission

router_core = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])
router_teach = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)],
                         tags=["teach"])

ARTIFACT_ALLOWLIST = {"meta.json", "stage_a.json", "extraction.json",
                      "findings.json", "report.json", "report.md", "sap_compare.json",
                      "web_evidence.json"}


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


# ---------------------------------------------------------------- check -------
def _run_pipeline(path: Path, doc_class: str, lang: str, use_vision: bool,
                  sap_image: Path | None = None, quality: bool = False,
                  web: bool = False) -> dict:
    mc.reset_host()
    with jobs.PIPELINE_LOCK:
        res = run_check(path, doc_class, use_vision=use_vision, lang=lang,
                        sap_image=sap_image, quality=quality,
                        web_evidence=True if web else None)
    report = json.loads(res.report_json)
    return {"run_id": res.run_id, "verdict": res.verdict, "report": report,
            "report_md": res.report_md}


@router_core.post("/check", tags=["check"])
def check(file: UploadFile | None = File(None), doc_class: str = Form("auto"),
          lang: str = Form("en"), use_vision: bool = Form(True),
          wait: bool = Form(True), sap_file: UploadFile | None = File(None),
          rerun_run_id: str = Form(""), quality: bool = Form(False),
          web: bool = Form(False)):
    if doc_class not in ("bank", "w9", "auto"):
        raise api_error(400, "bad_request", "doc_class must be 'bank', 'w9' or 'auto'")
    if web and os.environ.get("MDMDOC_MODE", "").strip() == "api-only":
        # the sealed BTP image promises NO outbound calls — the operator-console
        # click-opt-in does not exist there, so web=true must not slip through
        raise api_error(400, "bad_request",
                        "external web evidence is disabled in the api-only deployment")
    if lang not in ("en", "ru"):
        raise api_error(400, "bad_request", "lang must be 'en' or 'ru'")
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
        if doc_class == "w9":
            raise api_error(400, "bad_request", "SAP comparison applies to bank documents")
        sap_path = save_upload("sap__" + (sap_file.filename or "screen.png"),
                               sap_file.file.read())
    from ..estimate import estimate_seconds, human, sniff_text_layer
    est = estimate_seconds("bank" if doc_class == "auto" else doc_class,
                           sniff_text_layer(path), use_vision=use_vision,
                           sap=sap_path is not None, quality=quality)
    if wait:
        try:
            out = _run_pipeline(path, doc_class, lang, use_vision, sap_path, quality, web)
            out["estimate_s"] = est
            return out
        except UnreadableDocument as e:
            raise api_error(422, "unreadable_document", str(e))
        except mc.OllamaUnavailable as e:
            raise api_error(503, "model_host_down", str(e))

    def work(log):
        log(f"estimated duration: {human(est)}")
        log(f"document: {path.name}")
        if sap_path:
            log(f"SAP screenshot: {sap_path.name}")
        log(f"running {doc_class} pipeline{' (thorough tier)' if quality else ''}"
            f"{' + external web evidence' if web else ''}…")
        out = _run_pipeline(path, doc_class, lang, use_vision, sap_path, quality, web)
        out["estimate_s"] = est
        log(f"verdict: {out['verdict']} (run {out['run_id']})")
        return out

    job = jobs.REGISTRY.submit("check", work)
    from fastapi.responses import JSONResponse
    return JSONResponse({"job_id": job.id, "estimate_s": est}, status_code=202)


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


# ================================================================= teach =======
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
