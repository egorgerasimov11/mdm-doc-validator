"""`mdmdoc extract` — offline document extraction with the consensus guarantee.

Every page is read by independent local engines (the PDF text layer when it is
plausible, tesseract, RapidOCR, and a local vision model when Ollama is
available); the consensus layer decides per value whether it can be handed over
automatically (confirmed / checksum_ok) or must be shown to the operator
(review, with a crop of the page around it). Nothing leaves the machine.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import consensus as C, engines as E, render as R

DEFAULT_ENGINES = ["textlayer", "tess:auto", "rapidocr:auto"]

# Deterministic, offline document-type guess — the consensus transcript decides.
_DOC_TYPES: list[tuple[str, re.Pattern]] = [
    ("W-8BEN-E", re.compile(r"(?i)\bW-?8BEN-?E\b|certificate of status of beneficial owner")),
    ("W-8BEN", re.compile(r"(?i)\bW-?8BEN\b")),
    ("W-8ECI", re.compile(r"(?i)\bW-?8ECI\b")),
    ("W-8IMY", re.compile(r"(?i)\bW-?8IMY\b")),
    ("W-9", re.compile(r"(?i)\bW-?9\b|request for taxpayer identification number")),
    ("RIB (relevé d'identité bancaire)", re.compile(r"(?i)relev[ée]s? d'identit[ée] bancaire|\bRIB\b")),
    ("ACH / wire authorization form", re.compile(r"(?i)\bACH\b.*(?:form|authori[sz]ation)|wire (?:transfer )?(?:form|instructions)|EFT form")),
    ("voided check", re.compile(r"(?i)\bvoid(?:ed)?\b.*\bcheck\b|\bcheque\b.*\bvoid")),
    ("bank statement", re.compile(r"(?i)\bstatement\b.*\b(?:account|period)\b|afschrift|kontoauszug|relevé de compte|estratto conto|extracto")),
    ("bank confirmation letter", re.compile(r"(?i)kontobest[äa]tigung|bankbest[äa]tigung|confirm(?:s|ation)? (?:that )?.{0,40}account|certificaci[óo]n bancaria|attestation bancaire|bank(?:ing)? (?:details|letter)|to whom it may concern")),
    ("bankbook / passbook", re.compile(r"통장|계좌번호|預金通帳|存折")),
    ("tax registration certificate", re.compile(r"(?i)vat registration|tax registration|شهادة تسجيل|营业执照|开户许可证")),
    ("invoice", re.compile(r"(?i)\binvoice\b|\brechnung\b|\bfactur[ae]\b|\bfattura\b|請求書")),
]


def guess_doc_type(text: str) -> str:
    for name, rx in _DOC_TYPES:
        if rx.search(text or ""):
            return name
    return "unknown"


@dataclass
class PageExtract:
    page: int
    readings: dict = field(default_factory=dict)       # engine_id → text
    latency: dict = field(default_factory=dict)
    verdicts: list = field(default_factory=list)        # consensus.Verdict
    crops: dict = field(default_factory=dict)           # value → crop path
    lines: dict = field(default_factory=dict)           # engine_id → [{text,bbox}] for crops


def _engine_list(specs: list[str] | None, vlm: str | None) -> list[E.PageEngine]:
    specs = list(specs or DEFAULT_ENGINES)
    if vlm:
        # the benchmark winner reads at 200 dpi (v200); a bare model name gets that
        spec = vlm if vlm.startswith("ollama:") else f"ollama:{vlm}"
        if "@" not in spec:
            spec += "@v200"
        specs.append(spec)
    out = []
    for s in specs:
        eng = E.parse(s)
        ok, why = eng.available()
        if ok:
            out.append(eng)
        else:
            print(f"[extract] {eng.id} skipped: {why}", flush=True)
    if not out:
        raise RuntimeError("no engine available — install tesseract and `uv sync --group bench` (rapidocr)")
    return out


def _crop_for(value: str, page_img: Path, lines_by_engine: dict, out_dir: Path, idx: int) -> Path | None:
    """Crop the page around the first OCR line whose digits contain the value."""
    from PIL import Image
    needle = re.sub(r"\D", "", value.split(":", 1)[-1]) or value.split(":", 1)[-1]
    for lines in lines_by_engine.values():
        for ln in lines or []:
            body = re.sub(r"\D", "", ln.get("text") or "") if needle.isdigit() else (ln.get("text") or "")
            if needle and needle in body and ln.get("bbox"):
                x0, y0, x1, y1 = ln["bbox"]
                with Image.open(page_img) as im:
                    w, h = im.size
                    pad = max(12, (y1 - y0))
                    box = (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))
                    crop = im.crop(box)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    p = out_dir / f"p{idx}_{re.sub(r'[^A-Za-z0-9]+', '_', value)[:40]}.png"
                    crop.save(p)
                    return p
    return None


def extract_document(src: Path, *, engines: list[str] | None = None, vlm: str | None = None,
                     out_dir: Path | None = None, timeout: int = 300, pages: list[int] | None = None) -> dict:
    src = Path(src)
    out_dir = out_dir or (config.PROJECT_ROOT / "out" / "extract" / src.stem)
    cache = out_dir / "render"
    cache.mkdir(parents=True, exist_ok=True)
    engs = _engine_list(engines, vlm)
    n = R.page_count(src)
    page_ids = pages if pages is not None else list(range(n))
    t_start = time.time()
    page_results: list[PageExtract] = []
    for eng in engs:
        eng.setup()
    try:
        for idx in page_ids:
            pe = PageExtract(idx)
            for eng in engs:
                job = E.PageJob(src.stem, src, idx, cache, timeout_s=timeout)
                try:
                    res = eng.transcribe(job)
                except Exception as e:               # one engine failing must not lose the page
                    print(f"[extract] {eng.id} p{idx}: {e.__class__.__name__}: {str(e)[:120]}", flush=True)
                    continue
                if eng.family == "textlayer" and not (res.meta or {}).get("usable"):
                    continue                          # an implausible layer is not a voice
                pe.readings[eng.id] = res.text or ""
                pe.latency[eng.id] = res.latency_s
                if res.lines:
                    pe.lines[eng.id] = res.lines
            pe.verdicts = C.consensus(pe.readings)
            page_img = R.render_page(src, cache, idx, R.PRESETS["v200"])
            for v in pe.verdicts:
                if v.status == "review":
                    p = _crop_for(v.value, page_img, pe.lines, out_dir / "review", idx)
                    if p:
                        pe.crops[v.value] = p
            page_results.append(pe)
    finally:
        for eng in engs:
            try:
                eng.teardown()
            except Exception:
                pass
    full_text = "\n\n".join(E.merge_tile_texts(list(pe.readings.values())) for pe in page_results)
    doc = {
        "file": str(src), "pages": n, "pages_read": page_ids,
        "engines": [e.id for e in engs],
        "doc_type": guess_doc_type(full_text),
        "elapsed_s": round(time.time() - t_start, 1),
        "pages_out": [{"page": pe.page, "latency": pe.latency,
                       "values": [dict(v.as_dict(), crop=str(pe.crops.get(v.value, "")) or None)
                                  for v in pe.verdicts],
                       "readings": pe.readings} for pe in page_results],
        "transcript": full_text,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "extract.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    md = to_markdown(doc)
    (out_dir / "extract.md").write_text(md, encoding="utf-8")
    doc["out_dir"] = str(out_dir)
    return doc


def to_markdown(doc: dict) -> str:
    lines = [f"# {Path(doc['file']).name}", "",
             f"**Document type (guess):** {doc['doc_type']}  ",
             f"**Pages:** {doc['pages']} · **engines:** {', '.join(doc['engines'])} · "
             f"**time:** {doc['elapsed_s']} s · fully offline", ""]
    auto = [(p["page"], v) for p in doc["pages_out"] for v in p["values"] if v["status"] != "review"]
    rev = [(p["page"], v) for p in doc["pages_out"] for v in p["values"] if v["status"] == "review"]
    lines += ["## Values ready to use (confirmed by independent engines or by checksum)", ""]
    if auto:
        lines += ["| page | value | how | read by |", "|---|---|---|---|"]
        for pg, v in auto:
            lines.append(f"| {pg + 1} | `{v['value']}` | {v['status']} | {', '.join(v['families'])} |")
    else:
        lines.append("_none_")
    lines += ["", f"## Needs a human look ({len(rev)})", "",
              "Read by one engine only, engines disagree, or the glyphs are confusable — "
              "check against the crop before using.", ""]
    if rev:
        lines += ["| page | value | read by | crop |", "|---|---|---|---|"]
        for pg, v in rev:
            crop = f"![]({Path(v['crop']).name})" if v.get("crop") else "—"
            lines.append(f"| {pg + 1} | `{v['value']}` | {', '.join(v['voices'])} | {crop} |")
    else:
        lines.append("_none_")
    lines += ["", "## Transcript (union of the engines, for reading — not for copying values)", "",
              "```", doc["transcript"].strip(), "```", ""]
    return "\n".join(lines)


def cli_extract(a) -> int:
    rc = 0
    for p in a.path:
        src = Path(p).expanduser()
        if not src.exists():
            print(f"not found: {src}")
            rc = 2
            continue
        doc = extract_document(src, engines=a.engines.split(",") if a.engines else None, vlm=a.vlm,
                               out_dir=Path(a.out) / src.stem if a.out else None, timeout=a.timeout,
                               pages=[int(x) for x in a.pages.split(",")] if a.pages else None)
        n_auto = sum(1 for pg in doc["pages_out"] for v in pg["values"] if v["status"] != "review")
        n_rev = sum(1 for pg in doc["pages_out"] for v in pg["values"] if v["status"] == "review")
        print(f"{src.name}: {doc['doc_type']} · {doc['pages']} page(s) · {n_auto} values ready, "
              f"{n_rev} to review · {doc['elapsed_s']} s → {doc['out_dir']}/extract.md")
    return rc
