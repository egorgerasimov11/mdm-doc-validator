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


PRIMARY_ORDER = ("textlayer", "vlm", "rapidocr", "tesseract", "applevision")

KIND_LABELS = [      # (kind, group, label regex on the line's label text)
    ("IBAN", "bank", None),
    ("BIC / SWIFT", "bank", None),
    ("EIN", "tax", None),
    ("SSN", "tax", None),
    ("routing (ABA)", "bank", re.compile(r"(?i)\b(?:aba|routing|rtn)\b")),
    ("account", "bank", re.compile(r"(?i)account|compte|konto|cuenta|conto|계좌|口座|账号|賬號|счет|счёт|iban|acct")),
    ("bank code", "bank", re.compile(r"(?i)\b(?:banque|bank ?code|blz|bankleitzahl|guichet|branch|sort ?code|agence|clé|cle|key|bsb|ifsc|clabe|은행)\b")),
    ("tax id", "tax", re.compile(r"(?i)\b(?:tin|tax|vat|nif|cif|steuer|ust|partita|p\.?iva|inn|инн|кпп|огрн|rfc|cuit|ruc|rut|nit|gst|pan|abn|kvk|siret|siren|cégjegyzék|adószám|税|사업자)\b")),
    ("phone", "contact", re.compile(r"(?i)\b(?:tel|phone|fax|mobile|mobil|téléphone|telefon|telefono|☎|전화|电话|電話)\b")),
    ("postal code", "contact", re.compile(r"(?i)\b(?:zip|postal|plz|cp|cap|code postal|postcode|우편)\b")),
    ("date", "other", re.compile(r"(?i)\b(?:date|datum|fecha|data|일|日付|дата)\b")),
    ("reference", "other", re.compile(r"(?i)\b(?:ref|reference|invoice|facture|rechnung|order|no\.?|n°|nr|number|numero|número|id)\b")),
]
GROUP_ORDER = {"bank": 0, "tax": 1, "contact": 2, "other": 3}
GROUP_TITLES = {"bank": "Bank details", "tax": "Tax identifiers", "contact": "Contact", "other": "Other numbers"}


def family_rank(engine_id: str) -> int:
    from .consensus import family_of
    fam = family_of(engine_id)
    return PRIMARY_ORDER.index(fam) if fam in PRIMARY_ORDER else len(PRIMARY_ORDER)


def primary_reading(readings: dict[str, str]) -> tuple[str, str]:
    """One engine's transcript to SHOW — the most faithful reader available, never a
    merge: the text layer when it is usable, else the vision model, else RapidOCR,
    else tesseract. → (engine_id, text)."""
    best = None
    for eid, text in readings.items():
        if not (text or "").strip():
            continue
        if best is None or family_rank(eid) < family_rank(best[0]):
            best = (eid, text)
    return best or ("", "")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _locate(value: str, transcript: str) -> tuple[str, str, int]:
    """Find the value in the transcript → (label, pretty, line_index). `pretty` is
    the value as the document spells it (spaces, hyphens, country prefix), `label`
    what the document calls it. Every occurrence is scored and the best label wins:
    a table-header cell or a short label on the same line beats a long address line."""
    kind, _, val = value.partition(":")
    core = val or kind
    needle = _digits(core) if not val or kind in ("EIN", "SSN") else core.upper()
    lines = (transcript or "").split("\n")
    best: tuple[int, str, str, int] | None = None
    for i, ln in enumerate(lines):
        hay = _digits(ln) if needle.isdigit() else re.sub(r"\s", "", ln).upper()
        if not needle or needle not in hay:
            continue
        pretty, start = _pretty_span(ln, needle)
        before = ln[:start]
        cells = [c for c in _CELL_SPLIT.split(before) if c.strip()]
        same = re.sub(r"[\s:：|*_#-]+$", "", cells[-1] if cells else "").strip(" |*_#\t")
        same = re.sub(r"\s*[:：]\s*[A-Za-z]{1,3}$", "", same)      # "N° ADEME : FR" → "N° ADEME"
        prev = next((p for p in reversed(lines[:i]) if p.strip(" |*_#")), "")
        header = _header_cell(prev, ln, start)
        pv = prev.strip(" |*_#:")
        if header:
            score, label = 4, header
        elif same and len(same) <= 40 and not _looks_numeric(same):
            score, label = 3, same
        elif 0 < len(pv) <= 40 and not _looks_numeric(pv):
            score, label = 2, pv
        elif same and not _looks_numeric(same):
            score, label = 1, same[-60:]
        else:
            score, label = 0, ""
        if best is None or score > best[0]:
            best = (score, label, pretty or core, i)
    return (best[1], best[2], best[3]) if best else ("", core, -1)


def _looks_numeric(text: str) -> bool:
    t = re.sub(r"[\s|]", "", text or "")
    return bool(t) and sum(c.isdigit() for c in t) >= 0.6 * len(t)


_CELL_SPLIT = re.compile(r"\s{2,}|\s*\|\s*|\t")


def _header_cell(header: str, row: str, pos: int) -> str:
    """Header cell above column `pos` of `row`, when both lines split into the same
    number of cells (two-space / pipe separated)."""
    hc = [c for c in _CELL_SPLIT.split(header.strip(" |")) if c.strip()]
    rc = [c for c in _CELL_SPLIT.split(row.strip(" |")) if c.strip()]
    if len(hc) < 2 or len(hc) != len(rc):
        return ""
    # which cell of the row holds `pos`
    cursor = 0
    for k, cell in enumerate(rc):
        at = row.find(cell, cursor)
        if at <= pos < at + len(cell):
            if not _looks_numeric(cell):          # a header only sits above a DATA cell
                return ""
            h = hc[k].strip(" :")
            return h if 0 < len(h) <= 40 and not _looks_numeric(h) else ""
        cursor = at + len(cell)
    return ""


def _pretty_span(line: str, needle: str) -> tuple[str, int]:
    """Substring of `line` whose digits/letters equal `needle`, keeping separators."""
    keep = (lambda c: c.isdigit()) if needle.isdigit() else (lambda c: c.isalnum())
    idx = [k for k, c in enumerate(line) if keep(c)]
    flat = "".join(line[k] for k in idx).upper() if not needle.isdigit() else "".join(line[k] for k in idx)
    pos = flat.find(needle)
    if pos < 0:
        return "", 0
    start, end = idx[pos], idx[pos + len(needle) - 1] + 1
    return line[start:end], start


def classify(value: str, label: str) -> tuple[str, str]:
    kind, _, val = value.partition(":")
    if val:
        typed = {"IBAN": "IBAN", "SWIFT": "BIC / SWIFT", "EIN": "EIN", "SSN": "SSN"}[kind]
        return typed, "bank" if typed in ("IBAN", "BIC / SWIFT") else "tax"
    for name, group, rx in KIND_LABELS:
        if rx is not None and rx.search(label or ""):
            return name, group
    return "number", "other"


def _bbox_for(value: str, lines_by_engine: dict) -> tuple[list | None, dict]:
    core = value.split(":", 1)[-1]
    needle = _digits(core) if _digits(core) == _digits(value.partition(":")[2] or value) and not value.startswith(("IBAN:", "SWIFT:")) else core.upper()
    for lines in lines_by_engine.values():
        for ln in lines or []:
            t = ln.get("text") or ""
            hay = _digits(t) if needle.isdigit() else re.sub(r"\s", "", t).upper()
            if needle and needle in hay and ln.get("bbox"):
                return list(ln["bbox"]), ln
    return None, {}


def build_fields(verdicts: list, transcript: str, lines_by_engine: dict, page_size: tuple[int, int]) -> list[dict]:
    """Structured rows for one page: what the value is, what the document calls it,
    how it is spelled there, where it sits on the page (bbox in % of the v200 render)."""
    out = []
    for v in verdicts:
        label, pretty, line_no = _locate(v.value, transcript)
        kind, group = classify(v.value, label)
        bbox, _ = _bbox_for(v.value, lines_by_engine)
        pct = None
        if bbox and page_size[0] and page_size[1]:
            w, h = page_size
            pct = [round(bbox[0] / w * 100, 2), round(bbox[1] / h * 100, 2),
                   round(bbox[2] / w * 100, 2), round(bbox[3] / h * 100, 2)]
        out.append({"value": v.value, "pretty": pretty, "label": label, "kind": kind, "group": group,
                    "status": v.status, "voices": v.voices, "families": v.families,
                    "line": line_no, "bbox_pct": pct})
    # a review token that is only accepted values run together (tesseract reading a
    # table row "30003 02110 00037262223 29" as one number) is noise, not a value
    atoms = {_digits(f["value"].split(":", 1)[-1]) for f in out}
    atoms = {a for a in atoms if len(a) >= 4}
    out = [f for f in out if f["status"] != "review" or ":" in f["value"]
           or not _is_composite(_digits(f["value"]), atoms - {_digits(f["value"])})]
    order = {"confirmed": 0, "checksum_ok": 1, "review": 2}
    out.sort(key=lambda f: (GROUP_ORDER[f["group"]], order[f["status"]], f["kind"], f["value"]))
    return out


def _is_composite(digits: str, parts: set[str]) -> bool:
    """True when `digits` is a concatenation of >= 2 strings from `parts`, possibly
    with one short (<= 3 digit) leftover such as a RIB key."""
    if not digits or not parts:
        return False
    n = len(digits)
    best = [-1] * (n + 1)                      # best[i] = max parts covering digits[:i]; -1 = impossible
    best[0] = 0
    for i in range(n):
        if best[i] < 0:
            continue
        for pt in parts:
            if digits.startswith(pt, i) and best[i] + 1 > best[i + len(pt)]:
                best[i + len(pt)] = best[i] + 1
    for tail in range(0, 4):
        if n - tail > 0 and (best[n - tail] >= 2 or (best[n - tail] == 1 and tail > 0)):
            return True
    return False


@dataclass
class PageExtract:
    page: int
    readings: dict = field(default_factory=dict)       # engine_id → text
    latency: dict = field(default_factory=dict)
    verdicts: list = field(default_factory=list)        # consensus.Verdict
    crops: dict = field(default_factory=dict)           # value → crop path
    lines: dict = field(default_factory=dict)           # engine_id → [{text,bbox}] for crops
    primary: str = ""                                   # the transcript shown (one engine)
    primary_engine: str = ""
    fields: list = field(default_factory=list)
    size: tuple = (0, 0)


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
            hints: dict = {}
            # tesseract (multi-language CJK pass) runs first: its text tells the page's
            # scripts, which picks the RapidOCR dictionary and the VLM's CJK rescue
            for eng in sorted(engs, key=lambda e: 0 if e.family == "tess" else 1):
                job = E.PageJob(src.stem, src, idx, cache, hints=dict(hints), timeout_s=timeout)
                try:
                    res = eng.transcribe(job)
                except Exception as e:               # one engine failing must not lose the page
                    print(f"[extract] {eng.id} p{idx}: {e.__class__.__name__}: {str(e)[:120]}", flush=True)
                    continue
                if eng.family == "textlayer" and not (res.meta or {}).get("usable"):
                    continue                          # an implausible layer is not a voice
                pe.readings[eng.id] = res.text or ""
                pe.latency[eng.id] = res.latency_s
                if eng.family == "tess" and "scripts" not in hints:
                    hints["scripts"] = E.scripts_of_text(res.text or "")
                if res.lines:
                    pe.lines[eng.id] = res.lines
            pe.verdicts = C.consensus(pe.readings)
            page_img = R.render_page(src, cache, idx, R.PRESETS["v200"])
            from PIL import Image
            with Image.open(page_img) as im:
                pe.size = im.size
            pe.primary_engine, pe.primary = primary_reading(pe.readings)
            pe.fields = build_fields(pe.verdicts, pe.primary, pe.lines, pe.size)
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
    full_text = "\n\n".join(pe.primary for pe in page_results)
    union_text = "\n".join(E.merge_tile_texts(list(pe.readings.values())) for pe in page_results)
    doc = {
        "file": str(src), "pages": n, "pages_read": page_ids,
        "engines": [e.id for e in engs],
        "doc_type": guess_doc_type(union_text),          # any engine's words may name the form
        "elapsed_s": round(time.time() - t_start, 1),
        "pages_out": [{"page": pe.page, "latency": pe.latency,
                       "primary_engine": pe.primary_engine, "transcript": pe.primary,
                       "size": list(pe.size),
                       "fields": [dict(f, crop=str(pe.crops.get(f["value"], "")) or None) for f in pe.fields],
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


def grouped_fields(doc: dict) -> list[dict]:
    """[{group, title, rows:[field+page]}] in display order, empty groups dropped."""
    by: dict[str, list] = {}
    for pg in doc["pages_out"]:
        for f in pg.get("fields", []):
            by.setdefault(f["group"], []).append(dict(f, page=pg["page"] + 1))
    return [{"group": g, "title": GROUP_TITLES[g], "rows": by[g]}
            for g in sorted(by, key=lambda g: GROUP_ORDER[g])]


def to_markdown(doc: dict) -> str:
    lines = [f"# {Path(doc['file']).name}", "",
             f"**Document type (guess):** {doc['doc_type']}  ",
             f"**Pages:** {doc['pages']} · **engines:** {', '.join(doc['engines'])} · "
             f"**time:** {doc['elapsed_s']} s · fully offline", "",
             "Status: `confirmed` = read identically by independent engines · `checksum_ok` = one reading, "
             "its own check digit holds · `review` = look at the page before using.", ""]
    for grp in grouped_fields(doc):
        lines += [f"## {grp['title']}", "", "| page | field | value | status | read by |", "|---|---|---|---|---|"]
        for f in grp["rows"]:
            lines.append(f"| {f['page']} | {f['label'] or f['kind']} | `{f['pretty']}` | {f['status']} "
                         f"| {', '.join(f['families'])} |")
        lines.append("")
    n_rev = sum(1 for g in grouped_fields(doc) for f in g["rows"] if f["status"] == "review")
    if n_rev:
        lines += [f"_{n_rev} value(s) need a human look — crops are in `review/` next to this file._", ""]
    lines += ["## Transcript", "",
              "_One engine's reading of each page (" + ", ".join(
                  sorted({pg.get("primary_engine", "") for pg in doc["pages_out"]} - {""})) + "), verbatim._", "",
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
