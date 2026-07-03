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
                                      _ctx(doctor=doc, runs=rows[:40], page="dashboard",
                                           active_jobs=[j.to_dict() for j in jobs.REGISTRY.list()
                                                        if j.status in ("queued", "running")]))


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
    label = next((l for l in dataset.load_labels() if l.get("doc_sha256") == rid), None)
    return templates.TemplateResponse(request, "run.html", _ctx(
        page="runs", run_id=rid, meta=meta, pub=pub, findings=findings,
        report=rep, block=block, labeled=rid in _labeled_ids(), flash=flash,
        preview_pages=preview_pages, has_sap_shot=bool(meta.get("sap_path")),
        sap_rows=sap_rows, doc_class=meta.get("doc_class", "bank"), label=label,
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

    # structured last-eval results: failures with links, diff, field metrics
    last_results: dict = {}
    p = config.EVAL_DIR / "last_results.json"
    if p.exists():
        try:
            last_results = json.loads(p.read_text())
        except Exception:
            last_results = {}
    failures = [r for r in last_results.get("rows", [])
                if isinstance(r, dict) and ("error" in r or not r.get("ok", True))]
    diff = last_results.get("diff", {})
    cur = history[-1]["metrics"] if history else {}
    prev = history[-2]["metrics"] if len(history) > 1 else {}
    field_rows = [(k, v, (prev.get("fields") or {}).get(k))
                  for k, v in sorted((cur.get("fields") or {}).items())]

    # recommendations: the page should say what to do next, not just show numbers
    recs: list = []
    if cur:
        if cur.get("leakage_count"):
            recs.append(("bad", f"LEAKAGE {cur['leakage_count']} — fix before anything else"))
        if cur.get("invoice_false_accept_rate"):
            recs.append(("bad", "invoice false-accept > 0 — must be zero; review the invoice rules"))
        if diff.get("regressed"):
            recs.append(("bad", f"{len(diff['regressed'])} doc(s) regressed vs previous eval — "
                                "review them before adopting the model"))
        if failures:
            recs.append(("warn", f"{len(failures)} doc(s) not fully correct — label the ones "
                                 "below, they teach the most"))
        if len(labs) < 100:
            recs.append(("info", f"LoRA still gated: {len(labs)}/100 labels"))
        if not recs:
            recs.append(("ok", "clean eval — safe to adopt the current model/prompts"))
    try:
        current_model = mc.resolve("TEXT")
        strong_model = mc.resolve("TEXT_STRONG")
    except Exception:
        current_model, strong_model = "?", "?"

    return templates.TemplateResponse(request, "training.html", _ctx(
        page="training", by_class=by_class, history=history,
        series_json=json.dumps(series), lora_gate=100,
        failures=failures, diff=diff, field_rows=field_rows, recs=recs,
        last_tag=last_results.get("tag", ""), last_ts=last_results.get("ts", ""),
        current_model=current_model, strong_model=strong_model,
        running_jobs=[j.to_dict() for j in jobs.REGISTRY.list()
                      if j.status in ("queued", "running")]))


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
