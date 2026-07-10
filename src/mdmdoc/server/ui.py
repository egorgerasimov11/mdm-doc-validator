#!/usr/bin/env python3
"""ui.py — operator console pages (server-rendered Jinja2, Codex look)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import config, dataset, model_client as mc, ratings, review_core, runstore
from ..evidence import FIELD_EVIDENCE, resolve_all as resolve_evidence
from ..report import _data_rows
from . import jobs
from .api import _labeled_ids, doctor as api_doctor, eval_history

router_ui = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates" / "ui"))
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "ui"


def _asset_version() -> str:
    """Cache-bust key for app.js/app.css: newest file mtime. Without it a deploy
    leaves operators on a stale app.js and a newly-added init function (called
    inline on the run page) throws, breaking the page. Cheap stat, computed per
    render so a live edit is reflected immediately."""
    try:
        return str(int(max(os.path.getmtime(_STATIC_DIR / "app.js"),
                           os.path.getmtime(_STATIC_DIR / "app.css"))))
    except OSError:
        return "0"


def _ctx(**kw) -> dict:
    """Globals every page gets. `page` is the primary NAV GROUP (documents /
    rules / training / bulk / debug) and `subpage` the optional sub-tab — one
    route family can span several routes (a run page still lights 'Documents',
    approvals still lights 'Rules'). Template-only; nothing in Python reads them."""
    return {"token": os.environ.get("MDMDOC_API_TOKEN", ""),
            "labels_count": dataset.count_labels(),
            "asset_v": _asset_version(),
            "subpage": "",
            **kw}


def _field_confidence(prov: dict | None, engine_agree: bool | None) -> str:
    """Roll the already-recorded signals into one 3-level chip:
    'verified'   — deterministic source (ocr-regex/zone-probe/rule/precedent)
                   or a model read CONFIRMED by the OCR regex crosscheck;
    'agreed'     — dual mode saw both engines agree on the value;
    'check'      — dual mode saw the engines DISAGREE (look here first);
    'model-only' — a single-source model read, nothing corroborated it."""
    src = (prov or {}).get("source") or ""
    if src in ("ocr-regex", "zone-probe", "rule", "precedent", "vision-crop") \
            or (prov or {}).get("confirmed"):
        return "verified"
    if engine_agree is True:
        return "agreed"
    if engine_agree is False:
        return "check"
    return "model-only" if src == "model" else ""


def _doctor_safe() -> dict:
    try:
        return api_doctor()
    except Exception as e:  # page must render even if doctor explodes
        return {"model_host": {"reachable": False, "error": str(e)}, "roles": {},
                "tesseract": {}, "labels_count": 0, "runs_count": 0}


def _run_rows(test: bool) -> list[dict]:
    rows = runstore.list_runs(test=test)
    labeled = _labeled_ids()
    thumbs = ratings.latest()          # run_id -> up|down (one small jsonl read)
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    for r in rows:
        r["labeled"] = r["run_id"] in labeled
        r["rating"] = thumbs.get(r["run_id"], "")
    return rows


@router_ui.get("/ui", response_class=HTMLResponse)
def dashboard(request: Request):
    # no doctor probe here any more: the header chip fetches /doctor async
    # (refreshChip), so the page no longer blocks on the model host
    rows = _run_rows(test=False)
    env_eng = os.environ.get("MDMDOC_ENGINE", "").strip().lower()
    return templates.TemplateResponse(request, "dashboard.html",
                                      _ctx(runs=rows[:40], page="documents", subpage="documents",
                                           test_count=len(runstore.list_runs(test=True)),
                                           recent_actions=__import__("mdmdoc.oplog", fromlist=["recent"]).recent(limit=8),
                                           default_effort=config.default_effort(),
                                           engine_default=config.engine_mode(),
                                           engine_modes=list(config.ENGINE_MODES),
                                           engine_env_override=(env_eng if env_eng in config.ENGINE_MODES else ""),
                                           active_jobs=[j.to_dict() for j in jobs.REGISTRY.list()
                                                        if j.status in ("queued", "running")]))


@router_ui.get("/ui/test", response_class=HTMLResponse)
def test_runs_page(request: Request):
    """Pipeline exercises — the corpus, the synthetic set, anything analyzed
    outside the console. Same list, same filters, kept out of Documents."""
    rows = _run_rows(test=True)
    return templates.TemplateResponse(request, "test_runs.html",
                                      _ctx(runs=rows, page="documents", subpage="test",
                                           test_count=len(rows)))


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
    # which evidence crops are actually resolvable for this document — the
    # template only offers thumbnails for these (no broken images)
    try:
        evidence = resolve_evidence(rid)
    except Exception:
        evidence = {}
    prov = pub.get("provenance") or {}
    # dual-mode per-field agreement (engine_compare) feeds the confidence chip
    eng_agree = {r.get("field"): r.get("agree")
                 for r in (pub.get("engine_compare") or []) if isinstance(r, dict)}
    data_rows, seen_ev = [], set()
    if pub.get("fields") is not None:
        try:
            for row_label, value, hint, fkey in _data_rows(pub):
                ev = FIELD_EVIDENCE.get(fkey)
                ev = ev if ev in evidence and ev not in seen_ev else None
                if ev:
                    seen_ev.add(ev)
                # copy_value: the RAW field value for the clipboard — no display
                # suffixes like "(country IT, len 27)". Copy policy == display
                # policy: TIN kinds come back masked (there is no unmask path).
                data_rows.append({"label": row_label, "value": value, "hint": hint,
                                  "fkey": fkey,
                                  "copy_value": _display_field(pub.get("fields") or {}, fkey),
                                  "prov": prov.get(fkey), "ev": ev,
                                  "confidence": _field_confidence(
                                      prov.get(fkey), eng_agree.get(fkey))})
        except Exception:
            data_rows = []
    # only findings whose rule_id is a REAL rules.yaml rule get operator
    # actions (dispute/vote) — synthetic findings (RULE-GATE, ENGINE-*,
    # OPERATOR-*, CONF-001, PATTERN-1, SAP-*, TPL-*) are not editable rules
    import yaml as _yaml

    from .. import rules_io as _rules_io
    real_rule_ids: set = set()
    for _dc in ("bank", "w9"):
        try:
            _cfg = _yaml.safe_load(_rules_io.rules_text(_dc)) or {}
            real_rule_ids |= {str(r.get("id")) for r in _cfg.get("rules", []) or []
                              if isinstance(r, dict)}
        except Exception:
            pass
    for f in findings:
        if isinstance(f, dict):
            ev = FIELD_EVIDENCE.get(f.get("field") or "")
            f["ev"] = ev if ev in evidence else None
            f["real_rule"] = f.get("rule_id") in real_rule_ids
    stage_a_pub = runstore.load(rid, "stage_a.json") or {}
    preview_pages = _preview_pages(meta, stage_a_pub)
    sap_rows = runstore.load(rid, "sap_compare.json") or []
    tpl_rows = runstore.load(rid, "template_compare.json") or []
    web_rows = runstore.load(rid, "web_evidence.json") or []
    from ..web_enrichment import BANNER as web_banner
    label = next((l for l in dataset.load_labels() if l.get("doc_sha256") == rid), None)
    return templates.TemplateResponse(request, "run.html", _ctx(
        page="documents", run_id=rid, meta=meta, pub=pub, findings=findings,
        report=rep, data_rows=data_rows, labeled=rid in _labeled_ids(), flash=flash,
        preview_pages=preview_pages, total_pages=stage_a_pub.get("pages") or 1,
        has_sap_shot=bool(meta.get("sap_path")) and meta.get("sap_kind") != "table",
        sap_rows=sap_rows, tpl_rows=tpl_rows, web_rows=web_rows, web_banner=web_banner,
        rating=__import__("mdmdoc.ratings", fromlist=["of"]).of(rid),
        doc_class=meta.get("doc_class", "bank"), label=label,
        gate_stale=_gate_is_stale(findings, meta.get("doc_class", "bank")),
        pending_rules=meta.get("pending_rules") or [],
        challenge_counts=__import__("mdmdoc.challenges",
                                    fromlist=["counts"]).counts(),
        run_is_test=runstore.is_test(meta),
        trace=_learning_trace(label, pub, rep) if label else None,
        artifacts=["meta.json", "stage_a.json", "extraction.json", "reasoning.md", "findings.json",
                   "report.json", "report.md", "sap_compare.json", "web_evidence.json"]))


def _gate_is_stale(findings: list, doc_class: str) -> bool:
    """A verdict is a persisted artifact, not a live query: approving a rule does
    NOT re-decide a run that is already on disk. So a run held back by RULE-GATE
    keeps saying 'awaiting approval' forever, and the operator reasonably concludes
    the approvals never took. True here = the gate held this run and nothing in its
    class is pending any more; the page then asks for a re-analysis."""
    if not any((f or {}).get("rule_id") == "RULE-GATE" for f in findings):
        return False
    try:
        import yaml

        from .. import rule_approvals, rules_io
        store = rule_approvals.load()
        cfg = yaml.safe_load(rules_io.rules_text(doc_class)) or {}
        rules = cfg.get("rules") or []
        return bool(rules) and all(
            rule_approvals.status(store, doc_class, r) == rule_approvals.APPROVED
            for r in rules)
    except Exception:
        return False


def _preview_pages(meta: dict, stage_a_pub: dict) -> list[int]:
    """Pages worth showing: the ones the pipeline used, else the first few."""
    if not meta.get("path") or not Path(meta["path"]).exists():
        return []
    used = stage_a_pub.get("pages_used") or []
    total = stage_a_pub.get("pages") or 1
    pages = sorted(set(used))[:4] if used else list(range(min(total, 3)))
    return pages or [0]


def _display_field(pub_fields: dict, key: str) -> str:
    """Current displayable value of a diffed field (full/masked per policy)."""
    v = pub_fields.get("tin" if key == "tin_raw" else key, "")
    if isinstance(v, dict):
        if not v.get("present"):
            return ""
        return str(v.get("value") or v.get("masked") or "")
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v or "")


def _tail_eq(now: str, corrected: str) -> bool:
    """Gold values in labels are always masked while the console may show full
    values; every mask keeps the last 4 digits, so digit-bearing values compare
    by tail. Text values compare case-insensitively."""
    a, b = re.sub(r"\D", "", now), re.sub(r"\D", "", corrected)
    if len(a) >= 4 and len(b) >= 4:
        return a[-4:] == b[-4:]
    return now.strip().casefold() == corrected.strip().casefold()


def _learning_trace(label: dict, pub: dict, rep: dict) -> dict:
    """Field-level before -> corrected -> now. The trace must PROVE the machine
    learned the correction — a verdict that never changed proves nothing."""
    mp = label.get("model_predicted") or {}
    prec = pub.get("operator_precedent") or {}
    pub_fields = pub.get("fields") or {}
    rows, now_ok, now_bad = [], [], []
    for k, d in (mp.get("fields_diff") or {}).items():
        now = _display_field(pub_fields, k)
        corrected = str(d.get("gold") or "")
        ok = _tail_eq(now, corrected) if (now or corrected) else True
        rows.append({"field": k, "before": str(d.get("model") or "") or "—",
                     "corrected": corrected or "—", "now": now or "—", "ok": ok})
        (now_ok if ok else now_bad).append(k)
    return {
        "rows": rows, "now_ok": now_ok, "now_bad": now_bad,
        "type_changed": bool(mp.get("doc_type"))
                        and mp.get("doc_type") != label.get("doc_type_gold"),
        "verdict_changed": bool(prec.get("model_verdict"))
                           and prec.get("model_verdict") != label.get("verdict_gold"),
        "type_before": mp.get("doc_type") or prec.get("model_doc_type") or "—",
        "verdict_before": prec.get("model_verdict") or "—",
        "type_now_ok": rep.get("doc_type") == label.get("doc_type_gold"),
        "verdict_now_ok": rep.get("verdict") == label.get("verdict_gold"),
    }


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
        page="documents", form=form, preview_pages=_preview_pages(meta, stage_a_pub)))


@router_ui.get("/ui/training", response_class=HTMLResponse)
def training_page(request: Request):
    from .. import adoption
    from ..training_queue import build_queue
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
    scenario_rows = sorted((cur.get("scenarios") or {}).items())
    adoption_state = adoption.load_state()
    candidate = adoption_state.get("candidate")
    queue = build_queue()

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
        if candidate and not candidate.get("evaluated"):
            recs.append(("warn", "a candidate model is built but not evaluated — "
                                 "run the gated eval below"))
        elif candidate and candidate.get("gate_ok"):
            recs.append(("ok", "candidate passed the gate — adopt it below"))
        elif candidate:
            recs.append(("bad", "candidate FAILED the gate — see reasons below; "
                                "fix and rebuild, or keep the adopted model"))
        if queue:
            recs.append(("info", f"{len(queue)} document(s) in the training queue "
                                 "are worth labeling"))
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
        scenario_rows=scenario_rows, adoption=adoption_state, candidate=candidate,
        queue=queue,
        last_tag=last_results.get("tag", ""), last_ts=last_results.get("ts", ""),
        current_model=current_model, strong_model=strong_model,
        running_jobs=[j.to_dict() for j in jobs.REGISTRY.list()
                      if j.status in ("queued", "running")]))


@router_ui.get("/ui/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    """E2: every background job — running work survives navigation and lives
    here; finished work falls back to the persisted oplog after a restart."""
    from .. import oplog
    all_jobs = [j.to_dict() for j in jobs.REGISTRY.list()]
    active = [j for j in all_jobs if j["status"] in ("queued", "running")]
    finished = []
    for j in all_jobs:
        if j["status"] in ("done", "error", "canceled"):
            finished.append({"kind": j["kind"], "label": j.get("label"),
                             "status": j["status"], "finished": j.get("finished"),
                             "run_id": (j.get("result") or {}).get("run_id", "")})
    if len(finished) < 50:   # older history from the persisted ledger
        seen = {(f["kind"], f.get("finished")) for f in finished}
        for r in oplog.recent(limit=200, actions=("job-end",)):
            det = str(r.get("detail", ""))
            kind = det.split(" ", 1)[0] if det else "job"
            key = (kind, r.get("ts"))
            if key in seen:
                continue
            status = "done" if " done" in det else                 ("canceled" if " canceled" in det else
                 ("error" if " error" in det else ""))
            finished.append({"kind": kind, "label": det.split("— ")[-1] if "— " in det else "",
                             "status": status or "finished", "finished": "",
                             "ts": r.get("ts"), "run_id": ""})
            if len(finished) >= 50:
                break
    return templates.TemplateResponse(request, "activity.html", _ctx(
        page="activity", active=active, finished=finished[:50]))


@router_ui.get("/ui/history", response_class=HTMLResponse)
def history_page(request: Request):
    """E1: the operator action audit trail (dataset/oplog.jsonl); F1 adds the
    per-row Undo button (undone rows render struck-through)."""
    from .. import oplog, undo
    rows = oplog.recent(limit=500)
    undone = undo.undone_ops(rows)
    for r in rows:
        r["undone"] = r.get("op", "") in undone
        r["undoable"] = undo.can_undo(r, undone)
    return templates.TemplateResponse(request, "history.html", _ctx(
        page="activity", rows=rows))


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


@router_ui.get("/ui/rules", response_class=HTMLResponse)
def rules_page(request: Request, doc_class: str = "bank"):
    """Edit the verdict rules (rules/*.yaml) — the model never decides verdicts.
    Save writes the YAML; 'Regenerate for SAP' pushes them to the ABAP side."""
    from .. import rules_io
    doc_class = "w9" if doc_class == "w9" else "bank"
    text = rules_io.unified_text() or rules_io.rules_text(doc_class)
    unified = bool(rules_io.unified_text())
    n = 0
    try:
        import yaml
        if unified:
            n = sum(len((yaml.safe_load(b) or {}).get("rules") or [])
                    for b in rules_io._split_sections(text).values())
        else:
            n = len((yaml.safe_load(text) or {}).get("rules") or [])
    except Exception:  # rendering must never fail on bad YAML
        pass
    return templates.TemplateResponse(request, "rules.html", _ctx(
        page="rules", subpage="editor", doc_class=doc_class, yaml_text=text, rule_count=n,
        unified=unified))


@router_ui.get("/ui/rules/approve", response_class=HTMLResponse)
def rules_approve_page(request: Request, doc_class: str = "bank"):
    """Human sign-off on every rule (HARD GATE): only Approved rules fire; a
    pending applicable rule holds the document at NEED_MANUAL_REVIEW."""
    import yaml

    from .. import rule_approvals
    from .. import rules_io
    doc_class = "w9" if doc_class == "w9" else "bank"
    cfg = yaml.safe_load(rules_io.rules_text(doc_class) or "") or {}
    store = rule_approvals.load()
    from .. import challenges as _challenges
    ch_counts = _challenges.counts()
    rows = []
    for r in cfg.get("rules", []) or []:
        if not isinstance(r, dict):
            continue
        rows.append({"id": r.get("id"), "name": r.get("name"), "message": r.get("message"),
                     "severity": r.get("severity"), "verdict_effect": r.get("verdict_effect"),
                     "applies_to": r.get("applies_to"), "tier": r.get("tier") or "",
                     "source": str(r.get("source") or "").split(":")[0],
                     "source_full": str(r.get("source") or ""),
                     "challenges": int(ch_counts.get(str(r.get("id")), 0)),
                     "status": rule_approvals.status(store, doc_class, r)})
    counts = {s: sum(x["status"] == s for x in rows)
              for s in ("approved", "pending", "rejected")}
    # П7 tier-promotion proposals (best-effort: an empty runs dir just means none)
    proposals = []
    try:
        from .. import rule_stats
        payload = rule_stats.build()
        proposals = [p for p in payload.get("proposals", [])
                     if p.get("doc_class") == doc_class]
    except Exception:  # the approvals page must never fail on stats
        proposals = []
    # E4/E5: rules the operator's judgement has contradicted, with sample runs
    challenged = []
    for row in rows:
        if row["challenges"]:
            examples = _challenges.for_rule(str(row["id"]), limit=3)
            challenged.append({**row,
                               "runs": [e.get("run_id", "") for e in examples if e.get("run_id")],
                               "kinds": sorted({e.get("kind", "") for e in examples})})
    deleted = [d for d in rules_io.list_deleted()
               if d.get("doc_class") in ("", doc_class)]
    return templates.TemplateResponse(request, "rules_approve.html", _ctx(
        page="rules", subpage="approvals", doc_class=doc_class, rows=rows, counts=counts,
        proposals=proposals, challenged=challenged, deleted=deleted))


# --- /ui/tax — bulk US TIN validation tab (tin_bulk.py; tables shared with W9-040/041)

_TAX_DIR = config.RUNS_DIR / "tax"
_TAX_NAME = re.compile(r"^[\w.\-]+\.xlsx$")


def _tax_history() -> list[dict]:
    if not _TAX_DIR.exists():
        return []
    out = []
    for p in sorted(_TAX_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        st = p.stat()
        out.append({"name": p.name, "size": f"{st.st_size // 1024} KB",
                    "mtime": __import__("datetime").datetime.fromtimestamp(st.st_mtime)
                    .strftime("%Y-%m-%d %H:%M")})
    return out


@router_ui.get("/ui/bulk", response_class=HTMLResponse)
def bulk_page(request: Request):
    return templates.TemplateResponse(request, "bulk.html", _ctx(page="bulk", subpage="bulk"))


@router_ui.get("/ui/tax", response_class=HTMLResponse)
def tax_page(request: Request):
    return templates.TemplateResponse(request, "tax.html", _ctx(
        page="bulk", subpage="tax", result=None, error=None, history=_tax_history()))


@router_ui.post("/ui/tax/validate", response_class=HTMLResponse)
async def tax_validate(request: Request):
    import tempfile
    from datetime import datetime

    from .. import tin_bulk
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return templates.TemplateResponse(request, "tax.html", _ctx(
            page="bulk", subpage="tax", result=None, error="no file uploaded", history=_tax_history()))
    stem = re.sub(r"[^\w.\-]+", "_", Path(up.filename).stem)[:60] or "export"
    fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stem}_VALIDATED.xlsx"
    _TAX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=True) as tf:
            tf.write(await up.read())
            tf.flush()
            summary = tin_bulk.validate_workbook(
                Path(tf.name), _TAX_DIR / fname,
                sheet=str(form.get("sheet") or "").strip() or None,
                col=str(form.get("col") or "").strip() or None,
                id_col=str(form.get("id_col") or "").strip() or None,
                cat_col=str(form.get("cat_col") or "").strip() or None)
    except Exception as exc:  # surface as a page error, never a 500
        return templates.TemplateResponse(request, "tax.html", _ctx(
            page="bulk", subpage="tax", result=None,
            error=f"validation failed: {exc.__class__.__name__}: {exc}",
            history=_tax_history()))
    summary["fname"] = fname
    return templates.TemplateResponse(request, "tax.html", _ctx(
        page="bulk", subpage="tax", result=summary, error=None, history=_tax_history()))


@router_ui.get("/ui/tax/download/{name}")
def tax_download(name: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    if not _TAX_NAME.fullmatch(name):
        raise HTTPException(404)
    p = (_TAX_DIR / name).resolve()
    if p.parent != _TAX_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=name,
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")
