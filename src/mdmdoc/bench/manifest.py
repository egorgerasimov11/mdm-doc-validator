"""Corpus manifest for the transcription benchmark: bench/corpus.jsonl.

One JSON line per document. Identity is the sha256 of the file bytes
(doc_id = first 16 hex chars), so donors copied from several folders dedupe.

    {"doc_id": "…", "sha256": "…", "path": "doct/Bank account_Pf_Nam.PDF",
     "source_container": null, "kind": "scan", "ext": ".pdf", "pages_total": 1,
     "pages": [0], "langs": ["ko","en"], "scripts": ["Hangul","Latin"],
     "expected_doc_type": "bankbook", "tags": ["garbage_layer","core"],
     "stratum": "real", "gold_source": "claude", "added": "2026-08-21", "notes": ""}

`kind`: digital (usable text layer everywhere) | scan (image pages) | photo
(camera image file or phone capture) | mixed.  `stratum`: real | public | synthetic.
`gold_source`: claude (Agent SDK) | textlayer (synthetic docs — exact by construction).
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import fitz

from .. import config, ocr
from ..extract import render
from ..extract.plausibility import layer_usable, plausibility

def manifest_path() -> Path:
    return config.BENCH_DIR / "corpus.jsonl"


def extracted_dir() -> Path:
    return config.BENCH_DIR / "extracted"


DEFAULT_PAGES = 4
CONTAINER_EXTS = {".eml", ".zip", ".msg", ".xlsx", ".xlsm"}

SCRIPT_RES = {
    "Hangul": re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]"),
    "Kana": re.compile(r"[぀-ヿㇰ-ㇿ]"),
    "Han": re.compile(r"[一-鿿㐀-䶿豈-﫿]"),
    "Cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "Arabic": re.compile(r"[؀-ۿݐ-ݿ]"),
    "Hebrew": re.compile(r"[֐-׿]"),
    "Thai": re.compile(r"[฀-๿]"),
    "Devanagari": re.compile(r"[ऀ-ॿ]"),
    "Greek": re.compile(r"[Ͱ-Ͽ]"),
    "Latin": re.compile(r"[A-Za-zÀ-ɏ]"),
}


@dataclass
class Doc:
    doc_id: str
    sha256: str
    path: str
    ext: str
    kind: str = "scan"
    pages_total: int = 1
    pages: list[int] = field(default_factory=lambda: [0])
    langs: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    expected_doc_type: str = ""
    tags: list[str] = field(default_factory=list)
    stratum: str = "real"
    gold_source: str = "claude"
    source_container: str | None = None
    added: str = ""
    notes: str = ""
    sniff: dict = field(default_factory=dict)

    @property
    def abs_path(self) -> Path:
        p = Path(self.path).expanduser()
        return p if p.is_absolute() else (config.PROJECT_ROOT / p)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def render_dir(self) -> Path:
        return config.BENCH_DIR / "render" / self.doc_id

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Doc":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── helpers ───────────────────────────────────────────────────────────────────

def sha256_of(path: Path) -> str:
    from ..stage_a import _sha256
    return _sha256(path)


def rel_path(path: Path) -> str:
    p = path.expanduser().resolve()
    try:
        return str(p.relative_to(config.PROJECT_ROOT.resolve()))
    except ValueError:
        return str(p)


def detect_scripts(text: str) -> list[str]:
    counts = {name: len(rx.findall(text or "")) for name, rx in SCRIPT_RES.items()}
    total = sum(counts.values()) or 1
    # a script counts when it carries at least 2% of the letters and 4+ glyphs
    return [n for n, c in sorted(counts.items(), key=lambda kv: -kv[1])
            if c >= 4 and c / total >= 0.02]


def guess_langs(scripts: list[str]) -> list[str]:
    out: list[str] = []
    s = set(scripts)
    if "Hangul" in s:
        out.append("ko")
    if "Kana" in s:
        out.append("ja")
    elif "Han" in s:
        out.append("zh")
    if "Cyrillic" in s:
        out.append("ru")
    if "Arabic" in s:
        out.append("ar")
    if "Hebrew" in s:
        out.append("he")
    if "Thai" in s:
        out.append("th")
    if "Devanagari" in s:
        out.append("hi")
    if "Greek" in s:
        out.append("el")
    if "Latin" in s or not out:
        out.append("en")        # placeholder — refine by hand (--langs) for es/de/fr/…
    return out


def _pdf_sniff(path: Path) -> dict:
    """Per-page text-layer stats without OCR."""
    out = {"pages_total": 0, "layer_chars": [], "image_pages": 0, "garbage": False,
           "producer": "", "page_size": None}
    try:
        with fitz.open(path) as d:
            out["pages_total"] = d.page_count
            out["producer"] = (d.metadata or {}).get("producer", "") or ""
            texts = []
            for i in range(min(d.page_count, 12)):
                pg = d[i]
                t = pg.get_text() or ""
                texts.append(t)
                out["layer_chars"].append(len(t.strip()))
                if i == 0:
                    r = pg.rect
                    out["page_size"] = [round(r.width), round(r.height)]
                imgs = pg.get_images(full=False)
                if imgs and len(t.strip()) < 40:
                    out["image_pages"] += 1
            joined = "\n".join(texts)
            usable, why = layer_usable(joined)
            out["garbage"] = bool(ocr.text_layer_garbage(texts)) or (
                len(joined.strip()) >= 40 and not usable)
            out["layer_plausibility"] = plausibility(joined) if joined.strip() else None
            out["layer_reason"] = why
            out["text_sample"] = joined[:4000]
    except Exception as e:  # locked / broken
        out["error"] = str(e)[:200]
    return out


def sniff(path: Path) -> dict:
    """Automatic part of a manifest row (kind, scripts, langs, tags)."""
    path = path.expanduser()
    ext = path.suffix.lower()
    info: dict = {"ext": ext}
    tags: list[str] = []
    text = ""
    if render.is_image(path):
        info.update(pages_total=1, kind="photo")
        tags.append("image_file")
    else:
        s = _pdf_sniff(path)
        info.update(s)
        total = s.get("pages_total", 0) or 0
        chars = s.get("layer_chars", [])
        scanned = total and all(c < 40 for c in chars)
        sparse = total and (sum(1 for c in chars if c < 40) >= max(1, len(chars) // 2))
        if s.get("garbage"):
            info["kind"] = "scan"
            tags += ["garbage_layer"]
        elif scanned:
            info["kind"] = "scan"
        elif sparse:
            info["kind"] = "mixed"
            tags += ["sparse_layer"]
        else:
            info["kind"] = "digital"
            tags += ["text_layer"]
        ps = s.get("page_size") or [0, 0]
        if ps and ps[0] and abs(ps[0] - 595) > 60 and abs(ps[0] - 612) > 60 and abs(ps[0] - 842) > 60:
            tags.append("odd_page_size")     # phone captures / receipt strips
        if ps and ps[0] > ps[1] > 0:
            tags.append("landscape")
        if total > 1:
            tags.append("multipage")
        text = s.get("text_sample", "") if not s.get("garbage") else ""
    # scripts: from a usable text layer, else from a quick OCR of page 0
    if not text.strip() and ocr.HAVE_TESSERACT:
        try:
            from ..stage_a import _quick_ocr
            cache = config.BENCH_DIR / "render" / "_sniff" / sha256_of(path)[:16]
            q = render.render_page(path, cache, 0, render.PRESETS["q120"])
            text = _quick_ocr(str(q), force_cjk=False)
            rot = render.page_rotation(path, cache, 0)
            if rot:
                tags.append("rotated")
                info["rotation_p0"] = rot
            shutil.rmtree(cache, ignore_errors=True)
        except Exception:
            text = ""
    info["scripts"] = detect_scripts(text)
    info["langs"] = guess_langs(info["scripts"])
    if any(sc in info["scripts"] for sc in ("Hangul", "Kana", "Han")):
        tags.append("cjk")
    if "Arabic" in info["scripts"] or "Hebrew" in info["scripts"]:
        tags.append("rtl")
    info["tags"] = sorted(set(tags))
    info.pop("text_sample", None)
    info.pop("layer_chars", None)
    return info


# ── persistence ───────────────────────────────────────────────────────────────

def load_all() -> list[Doc]:
    mp = manifest_path()
    if not mp.exists():
        return []
    out = []
    for line in mp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(Doc.from_json(json.loads(line)))
    return out


def save_all(docs: list[Doc]) -> None:
    mp = manifest_path()
    mp.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(d.to_json(), ensure_ascii=False) + "\n" for d in docs)
    config.atomic_write_text(mp, body)


def _match_clause(d: Doc, clause: str) -> bool:
    clause = clause.strip()
    if not clause or clause == "all":
        return True
    if ":" not in clause:
        return clause in d.tags or clause == d.doc_id or clause in d.name
    key, val = clause.split(":", 1)
    key, val = key.strip(), val.strip()
    if key == "tag":
        return val in d.tags
    if key == "lang":
        return val in d.langs
    if key == "kind":
        return d.kind == val
    if key == "stratum":
        return d.stratum == val
    if key in ("sha", "id"):
        return d.doc_id.startswith(val) or d.sha256.startswith(val)
    if key == "name":
        return val.lower() in d.name.lower()
    if key == "script":
        return val in d.scripts
    if key == "type":
        return val.lower() in (d.expected_doc_type or "").lower()
    if key == "not":
        return not _match_clause(d, val)
    raise ValueError(f"unknown filter key {key!r} in {clause!r}")


def matches(d: Doc, expr: str) -> bool:
    """'a,b&c' = (a OR b) AND c.  Clauses: all | tag:x | lang:x | kind:x | stratum:x |
    sha:prefix | id:prefix | name:substr | script:x | type:substr | not:<clause> | <tag>."""
    for group in (expr or "all").split("&"):
        if not any(_match_clause(d, c) for c in group.split(",")):
            return False
    return True


def load(expr: str = "all") -> list[Doc]:
    return [d for d in load_all() if matches(d, expr)]


def get(doc_id: str) -> Doc | None:
    for d in load_all():
        if d.doc_id == doc_id or d.doc_id.startswith(doc_id):
            return d
    return None


# ── adding documents ──────────────────────────────────────────────────────────

def _expand_container(path: Path) -> list[tuple[Path, str]]:
    """Extract the documents of an eml/zip/msg/xlsx into bench/extracted/<sha>/.
    Returns [(member_path, container_label)]."""
    sha = sha256_of(path)[:16]
    out = extracted_dir() / sha
    out.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    members: list[Path] = []
    if ext == ".eml":
        import email
        from email import policy
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        for part in msg.walk():
            fn = part.get_filename()
            if not fn:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload or len(payload) < 10_000:
                continue
            p = out / Path(fn).name.replace("/", "_")
            p.write_bytes(payload)
            members.append(p)
    elif ext == ".zip":
        import zipfile
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
        members = [p for p in out.rglob("*") if p.is_file() and not p.name.startswith("._")]
    elif ext == ".msg":
        from .. import office_embed
        members = list(office_embed.extract_msg_attachments(path, out) or [])
    elif ext in (".xlsx", ".xlsm"):
        from .. import office_embed
        members = list(office_embed.extract_workbook_embeddings(path, out) or [])
    docs = [m for m in members
            if m.suffix.lower() == ".pdf" or render.is_image(m)]
    return [(m, rel_path(path)) for m in docs]


def add(paths, *, kind: str | None = None, langs: list[str] | None = None, tags=(),
        expected_doc_type: str = "", pages: list[int] | None = None, stratum: str = "real",
        notes: str = "", gold_source: str = "claude") -> list[Doc]:
    """Add/update documents. Manual fields win over sniffed ones; re-adding the
    same sha updates the row in place (keeps its doc_id and gold)."""
    existing = {d.sha256: d for d in load_all()}
    order = [d.sha256 for d in existing.values()]
    added: list[Doc] = []
    todo: list[tuple[Path, str | None]] = []
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if p.is_dir():
            for q in sorted(p.iterdir()):
                if q.is_file() and not q.name.startswith(".") and (
                        q.suffix.lower() == ".pdf" or render.is_image(q) or q.suffix.lower() in CONTAINER_EXTS):
                    todo.append((q, None))
            continue
        if not p.exists():
            raise FileNotFoundError(str(p))
        if p.suffix.lower() in CONTAINER_EXTS:
            todo.extend(_expand_container(p))
        else:
            todo.append((p, None))
    for p, container in todo:
        sha = sha256_of(p)
        info = sniff(p)
        prev = existing.get(sha)
        total = int(info.get("pages_total") or 1)
        auto_pages = list(range(min(total, DEFAULT_PAGES)))
        d = Doc(
            doc_id=sha[:16], sha256=sha, path=rel_path(p), ext=info.get("ext", p.suffix.lower()),
            kind=kind or (prev.kind if prev and prev.kind else info.get("kind", "scan")),
            pages_total=total,
            pages=pages or (prev.pages if prev else auto_pages),
            langs=langs or (prev.langs if prev and prev.langs else info.get("langs", [])),
            scripts=info.get("scripts", []),
            expected_doc_type=expected_doc_type or (prev.expected_doc_type if prev else ""),
            tags=sorted(set((prev.tags if prev else []) + list(tags) + info.get("tags", []))),
            stratum=stratum if stratum != "real" or not prev else prev.stratum,
            gold_source=gold_source if gold_source != "claude" or not prev else prev.gold_source,
            source_container=container or (prev.source_container if prev else None),
            added=prev.added if prev and prev.added else date.today().isoformat(),
            notes=notes or (prev.notes if prev else ""),
            sniff={k: v for k, v in info.items() if k in ("producer", "page_size", "image_pages",
                                                           "garbage", "layer_plausibility",
                                                           "rotation_p0", "error")},
        )
        if sha not in existing:
            order.append(sha)
        existing[sha] = d
        added.append(d)
    save_all([existing[s] for s in order])
    return added


def build_synthetic() -> int:
    """Add eval/synthetic/docs/*.pdf — their text layer IS the gold (synth.py
    writes Helvetica text via insert_text)."""
    labels_path = config.EVAL_DIR / "synthetic" / "labels.jsonl"
    docs_dir = config.EVAL_DIR / "synthetic" / "docs"
    scen: dict[str, list[str]] = {}
    types: dict[str, str] = {}
    if labels_path.exists():
        for line in labels_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            scen[Path(r["doc_path"]).name] = list(r.get("scenarios") or [])
            types[Path(r["doc_path"]).name] = r.get("doc_type_gold", "")
    n = 0
    for pdf in sorted(docs_dir.glob("*.pdf")):
        tags = ["synthetic"] + [s.replace("synth_", "") for s in scen.get(pdf.name, [])]
        layer = ""
        try:
            with fitz.open(pdf) as d:
                layer = "".join(d[i].get_text() for i in range(d.page_count))
        except Exception:
            pass
        if any("lang_zh" in t or "lang_ja" in t or "lang_ko" in t for t in tags) \
                and ocr.cjk_char_count(layer) < 4:
            tags.append("synthetic_cjk_suspect")
        add([pdf], tags=tags, expected_doc_type=types.get(pdf.name, ""),
            stratum="synthetic", gold_source="textlayer",
            pages=list(range(min(render.page_count(pdf), DEFAULT_PAGES))))
        n += 1
    return n


def materialize(dest: Path | None = None) -> int:
    """Copy every real document into bench/corpus/<doc_id>__<name> and point the
    manifest at that copy (relative path) so the corpus travels with bench/ to
    another machine (the Mac mini runs the sweeps). Idempotent."""
    import shutil as _sh
    dest = dest or (config.BENCH_DIR / "corpus")
    dest.mkdir(parents=True, exist_ok=True)
    docs = load_all()
    n = 0
    for d in docs:
        if d.stratum == "synthetic":
            continue                      # lives in the repo (eval/synthetic/docs)
        src = d.abs_path
        base = Path(d.path).name
        if base.startswith(f"{d.doc_id}__"):          # already materialised — do not re-prefix
            base = base[len(d.doc_id) + 2:]
        target = dest / f"{d.doc_id}__{base}"
        if not target.exists():
            if not src.exists():
                continue
            _sh.copy2(src, target)
        if d.path != rel_path(target):
            d.notes = (d.notes + f" | origin: {d.path}").strip(" |") if "origin:" not in d.notes else d.notes
            d.path = rel_path(target)
            n += 1
    save_all(docs)
    return n


def text_layer(doc: Doc, idx: int) -> str:
    """The PDF text layer of one page in reading order ('' for images)."""
    if render.is_image(doc.abs_path):
        return ""
    try:
        with fitz.open(doc.abs_path) as d:
            return d[idx].get_text("text", sort=True) or ""
    except Exception:
        return ""


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")
