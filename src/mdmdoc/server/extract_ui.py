"""/ui/extract — the offline consensus extractor as a page: upload documents, watch
the job, read the values by status with the page crops. Beta for manual testing."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config
from .deps import api_error, save_upload
from .jobs import REGISTRY

router_extract = APIRouter(include_in_schema=False)
OUT_ROOT = config.PROJECT_ROOT / "out" / "extract"
DEFAULT_VLM = os.environ.get("MDMDOC_EXTRACT_VLM", "qwen2.5vl:7b")   # "" disables the VLM voice
_SAFE = re.compile(r"^[A-Za-z0-9._ \-]+$")


def _templates():
    from .ui import templates
    return templates


def _ctx(**kw):
    from .ui import _ctx as base_ctx
    return base_ctx(page="extract", **kw)


def _results() -> list[dict]:
    rows = []
    if OUT_ROOT.exists():
        for d in sorted(OUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            f = d / "extract.json"
            if not f.exists():
                continue
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            vals = [v for pg in doc.get("pages_out", []) for v in (pg.get("fields") or pg.get("values", []))]
            rows.append({"name": d.name, "file": Path(doc.get("file", d.name)).name,
                         "doc_type": doc.get("doc_type", "?"), "pages": doc.get("pages"),
                         "elapsed_s": doc.get("elapsed_s"), "engines": doc.get("engines", []),
                         "ready": sum(1 for v in vals if v["status"] != "review"),
                         "review": sum(1 for v in vals if v["status"] == "review")})
    return rows


def _jobs() -> list[dict]:
    return [j.to_dict() for j in REGISTRY.list() if j.kind == "extract"][:20]


@router_extract.get("/ui/extract", response_class=HTMLResponse)
def extract_page(request: Request, flash: str = ""):
    return _templates().TemplateResponse(request, "extract.html", _ctx(
        results=_results(), jobs=_jobs(), vlm=DEFAULT_VLM, flash=flash))


@router_extract.post("/ui/extract/upload")
async def extract_upload(files: list[UploadFile] = File(...), vlm: str = Form("")):
    from ..extract.extractor import extract_document
    names = []
    for up in files:
        data = await up.read()
        path = save_upload(up.filename or "document", data)
        use_vlm = (vlm or DEFAULT_VLM).strip() or None
        if use_vlm == "none":
            use_vlm = None

        def run(log, job, path=path, use_vlm=use_vlm):
            job.label = path.name
            job.stage = "reading"
            log(f"{path.name}: text layer + tesseract + RapidOCR"
                + (f" + {use_vlm} (Ollama, if reachable)" if use_vlm else ""))
            # save_upload() names the file <sha16>__<name>; the result dir keeps the
            # original name so the list reads like the operator's folder
            stem = Path(path.name.split("__", 1)[-1]).stem or path.stem
            doc = extract_document(path, vlm=use_vlm, out_dir=OUT_ROOT / stem)
            n = sum(len(pg["values"]) for pg in doc["pages_out"])
            log(f"done: {doc['doc_type']} · {doc['pages']} page(s) · {n} values · {doc['elapsed_s']} s")
            return {"name": stem}

        REGISTRY.submit("extract", run, pass_job=True)
        names.append(path.name)
    return RedirectResponse(f"/ui/extract?flash=queued+{len(names)}", status_code=303)


@router_extract.get("/ui/extract/jobs")
def extract_jobs():
    return {"jobs": _jobs(), "results": _results()[:5]}


def _upgrade(doc: dict) -> dict:
    """Results written before the structured output (no `fields`, merged
    transcript) are rebuilt from the stored engine readings — the consensus and
    the labels are pure functions of those; only the page boxes are lost."""
    from ..extract import consensus as C
    from ..extract.extractor import build_fields, primary_reading
    changed = False
    for pg in doc.get("pages_out", []):
        if "fields" in pg:
            continue
        readings = pg.get("readings") or {}
        eng, primary = primary_reading(readings)
        verdicts = C.consensus(readings)
        pg["primary_engine"], pg["transcript"] = eng, primary
        pg["fields"] = build_fields(verdicts, primary, {}, (0, 0))
        changed = True
    if changed:
        doc["transcript"] = "\n\n".join(pg.get("transcript", "") for pg in doc.get("pages_out", []))
    return doc


def _load(name: str) -> dict:
    if not _SAFE.match(name):
        raise api_error(400, "bad_request", "bad name")
    f = OUT_ROOT / name / "extract.json"
    if not f.exists():
        raise api_error(404, "not_found", "no such extraction")
    return _upgrade(json.loads(f.read_text(encoding="utf-8")))


@router_extract.get("/ui/extract/{name}", response_class=HTMLResponse)
def extract_result(request: Request, name: str):
    from ..extract.extractor import grouped_fields, transcript_lines_with_boxes
    doc = _load(name)
    groups = grouped_fields(doc)
    for g in groups:
        for f in g["rows"]:
            f["crop"] = Path(f["crop"]).name if f.get("crop") else ""
    n_ready = sum(1 for g in groups for f in g["rows"] if f["status"] != "review")
    n_review = sum(1 for g in groups for f in g["rows"] if f["status"] == "review")
    pages = [pg["page"] for pg in doc["pages_out"]]
    return _templates().TemplateResponse(request, "extract_result.html", _ctx(
        name=name, doc=doc, groups=groups, n_ready=n_ready, n_review=n_review, pages=pages,
        file=Path(doc["file"]).name, short=Path(doc["file"]).name.split("__", 1)[-1],
        transcripts=[(pg["page"], pg.get("primary_engine", ""),
                      transcript_lines_with_boxes(pg.get("transcript", ""), pg.get("boxes") or []))
                     for pg in doc["pages_out"]]))


@router_extract.get("/ui/extract/{name}/page/{page}")
def extract_page_image(name: str, page: int):
    """The page as the extractor saw it (the v200 render the bboxes refer to),
    streamed for the viewer — never stored anywhere else."""
    from ..extract import render as R
    doc = _load(name)
    src = Path(doc["file"])
    if not src.exists():
        raise api_error(404, "not_found", "source document is gone")
    if page < 0 or page >= int(doc.get("pages") or 1):
        raise api_error(404, "not_found", "page out of range")
    img = R.render_page(src, OUT_ROOT / name / "render", page, R.PRESETS["v200"])
    return FileResponse(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router_extract.get("/ui/extract/{name}/crop/{crop}")
def extract_crop(name: str, crop: str):
    if not (_SAFE.match(name) and _SAFE.match(crop)):
        raise api_error(400, "bad_request", "bad name")
    p = OUT_ROOT / name / "review" / crop
    if not p.exists():
        raise api_error(404, "not_found", "no crop")
    return FileResponse(p, media_type="image/png")


@router_extract.get("/ui/extract/{name}/markdown")
def extract_markdown(name: str):
    if not _SAFE.match(name):
        raise api_error(400, "bad_request", "bad name")
    p = OUT_ROOT / name / "extract.md"
    if not p.exists():
        raise api_error(404, "not_found", "no markdown")
    return FileResponse(p, media_type="text/markdown; charset=utf-8", filename=f"{name}.md")
