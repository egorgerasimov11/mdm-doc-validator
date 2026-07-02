#!/usr/bin/env python3
"""ui.py — operator console pages (server-rendered Jinja2, Codex look)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import config, dataset, model_client as mc, review_core, runstore
from ..report import data_block
from . import jobs
from .api import _labeled_ids, doctor as api_doctor, eval_history

router_ui = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates" / "ui"))


def _ctx(**kw) -> dict:
    return {"token": os.environ.get("MDMDOC_API_TOKEN", ""),
            "labels_count": dataset.count_labels(),
            **kw}


def _doctor_safe() -> dict:
    try:
        return api_doctor()
    except Exception as e:  # page must render even if doctor explodes
        return {"model_host": {"reachable": False, "error": str(e)}, "roles": {},
                "tesseract": {}, "labels_count": 0, "runs_count": 0}


@router_ui.get("/ui", response_class=HTMLResponse)
def dashboard(request: Request):
    doc = _doctor_safe()
    rows = runstore.list_runs()
    labeled = _labeled_ids()
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    for r in rows:
        r["labeled"] = r["run_id"] in labeled
    return templates.TemplateResponse(request, "dashboard.html",
                                      _ctx(doctor=doc, runs=rows[:20], page="dashboard"))


@router_ui.get("/ui/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str, flash: str = ""):
    rid = runstore.resolve_run(run_id)
    if not rid:
        return templates.TemplateResponse(request, "missing.html", _ctx(what=run_id),
                                          status_code=404)
    meta = runstore.load(rid, "meta.json") or {}
    pub = runstore.load(rid, "extraction.json") or {}
    findings = runstore.load(rid, "findings.json") or []
    rep = runstore.load(rid, "report.json")
    if isinstance(rep, str):
        rep = json.loads(rep)
    rep = rep or {}
    block = ""
    if pub.get("fields") is not None:
        try:
            block = data_block(pub)
        except Exception:
            block = ""
    stage_a_pub = runstore.load(rid, "stage_a.json") or {}
    preview_pages = _preview_pages(meta, stage_a_pub)
    sap_rows = runstore.load(rid, "sap_compare.json") or []
    return templates.TemplateResponse(request, "run.html", _ctx(
        page="runs", run_id=rid, meta=meta, pub=pub, findings=findings,
        report=rep, block=block, labeled=rid in _labeled_ids(), flash=flash,
        preview_pages=preview_pages, has_sap_shot=bool(meta.get("sap_path")),
        sap_rows=sap_rows, doc_class=meta.get("doc_class", "bank"),
        artifacts=["meta.json", "stage_a.json", "extraction.json", "findings.json",
                   "report.json", "report.md", "sap_compare.json"]))


def _preview_pages(meta: dict, stage_a_pub: dict) -> list[int]:
    """Pages worth showing: the ones the pipeline used, else the first few."""
    if not meta.get("path") or not Path(meta["path"]).exists():
        return []
    used = stage_a_pub.get("pages_used") or []
    total = stage_a_pub.get("pages") or 1
    pages = sorted(set(used))[:4] if used else list(range(min(total, 3)))
    return pages or [0]


@router_ui.get("/ui/runs/{run_id}/review", response_class=HTMLResponse)
def review_page(request: Request, run_id: str):
    try:
        form = review_core.review_defaults(run_id)
    except review_core.RunNotFound:
        return templates.TemplateResponse(request, "missing.html", _ctx(what=run_id),
                                          status_code=404)
    meta = runstore.load(form["run_id"], "meta.json") or {}
    stage_a_pub = runstore.load(form["run_id"], "stage_a.json") or {}
    return templates.TemplateResponse(request, "review.html", _ctx(
        page="runs", form=form, preview_pages=_preview_pages(meta, stage_a_pub)))


@router_ui.get("/ui/training", response_class=HTMLResponse)
def training_page(request: Request):
    labs = dataset.load_labels()
    by_class: dict = {}
    for l in labs:
        by_class[l.get("doc_class", "?")] = by_class.get(l.get("doc_class", "?"), 0) + 1
    history = eval_history()
    headline = ["doc_type_accuracy", "verdict_accuracy", "json_valid_first_try", "leakage_count"]
    series = {m: [h["metrics"].get(m) for h in history if h.get("metrics")] for m in headline}
    return templates.TemplateResponse(request, "training.html", _ctx(
        page="training", by_class=by_class, history=history,
        series_json=json.dumps(series), lora_gate=100))


@router_ui.get("/ui/debug", response_class=HTMLResponse)
def debug_page(request: Request):
    doc = _doctor_safe()

    def _dir_size(p: Path) -> str:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0
        return f"{total / 1e6:.1f} MB"

    sizes = {"runs/": _dir_size(config.RUNS_DIR), "inbox/": _dir_size(config.INBOX_DIR),
             "dataset/": _dir_size(config.DATASET_DIR), "eval/": _dir_size(config.EVAL_DIR)}
    return templates.TemplateResponse(request, "debug.html", _ctx(
        page="debug", doctor=doc, doctor_json=json.dumps(doc, indent=2, ensure_ascii=False),
        jobs=[j.to_dict() for j in jobs.REGISTRY.list()], sizes=sizes,
        log_lines=list(jobs.LOG_RING)[-200:]))
