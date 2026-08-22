"""Page engines — the pluggable transcription layer shared by the benchmark
and the extractor.

An engine turns ONE rendered page into text (PageResult). Engines are built
from a spec string:

    family:model[@render][#prompt][~mod…]

    textlayer                         PDF text layer (PyMuPDF, reading order)
    tess:auto | tess:kor+eng~psm6     tesseract (auto = production CJK-retry logic)
    rapidocr:auto | rapidocr:korean   RapidOCR (PaddleOCR models on ONNX Runtime, CPU,
                                      fully offline; auto = pick the rec model by the
                                      document's scripts, then by confidence)
    applevision:legacy | :document    Apple Vision via tools/visionocr (Swift CLI)
    ollama:qwen2.5vl:7b@v200#transcribe_md.v1~tiles:q4~ocrhint~twopass
    mlx:mlx-community/Qwen3-VL-8B-Instruct-4bit@v200      (tools/mlxvlm worker)

Modifiers: tiles:h2|q4|r3x2 (read tiles, merge), ocrhint (feed a tesseract
draft), twopass (second call that verifies against the image),
langs=en-US+ko-KR (applevision), psmN (tesseract).

Engine.version folds the prompt text into the cache key: editing a prompt
invalidates exactly the cells that used it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, ocr
from . import render as R
from .loops import collapse_repeats, looks_looped
from .plausibility import layer_usable, plausibility

# Bump ONLY for code changes that alter what every engine family produces. Generation
# options (temperature, repeat_penalty, num_predict …) are folded per family via
# _opts_sha8() so that changing an Ollama option invalidates Ollama cells and nothing
# else — bumping this for the repeat_penalty change threw away 900 valid textlayer/
# tesseract/applevision cells for no reason.
CODE_VERSION = "1"


def _opts_sha8(opts: dict) -> str:
    """Stable 8-hex digest of a generation-options dict (part of a VLM engine's version)."""
    return hashlib.sha256(json.dumps(opts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]


# ── data ──────────────────────────────────────────────────────────────────────

@dataclass
class PageJob:
    doc_id: str
    src: Path              # PDF or image file
    page: int              # 0-based
    cache_dir: Path        # render cache for this document
    hints: dict = field(default_factory=dict)   # e.g. {"ocr_text": "..."} from another engine
    timeout_s: int = 300


@dataclass
class PageResult:
    text: str = ""
    lines: list[dict] | None = None
    markdown: str | None = None
    latency_s: float = 0.0
    meta: dict = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return {"text": self.text, "lines": self.lines, "markdown": self.markdown,
                "latency_s": self.latency_s, "meta": self.meta, "error": self.error}


class EngineUnavailable(RuntimeError):
    pass


# ── prompts ───────────────────────────────────────────────────────────────────

def _prompt_dir() -> Path:
    return config.VISION_PROMPTS_DIR


def resolve_prompt(name: str) -> tuple[str, str, str]:
    """'transcribe_md' → highest version; 'transcribe_md.v1' → pinned.
    Returns (text, tag, sha8)."""
    d = _prompt_dir()
    m = re.fullmatch(r"(.+?)\.v(\d+)", name)
    if m:
        path = d / f"{name}.txt"
        if not path.exists():
            raise FileNotFoundError(f"prompt {path} not found")
    else:
        cands = sorted(d.glob(f"{name}.v*.txt"),
                       key=lambda p: int(re.search(r"\.v(\d+)\.txt$", p.name).group(1)))
        if not cands:
            raise FileNotFoundError(f"no prompt files {name}.v*.txt under {d}")
        path = cands[-1]
    text = path.read_text(encoding="utf-8")
    return text, path.stem, hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# ── base ──────────────────────────────────────────────────────────────────────

# Where an engine can actually run. "abap" = reachable from the SAP ABAP twin
# (ZMDMDOC): it has its own pure-ABAP PDF text layer and an Ollama HTTP client,
# and nothing else — no tesseract binary, no Apple frameworks, no Python.
PLATFORMS_ALL = ("macos", "windows", "linux")


class PageEngine:
    family = "base"
    render: R.RenderSpec = R.PRESETS["g300"]
    id: str = "base"
    version: str = CODE_VERSION
    default_timeout_s = 120
    platforms: tuple[str, ...] = PLATFORMS_ALL

    def available(self) -> tuple[bool, str]:
        return True, ""

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def transcribe(self, job: PageJob) -> PageResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def page_image(self, job: PageJob, spec: R.RenderSpec | None = None) -> Path:
        return R.render_page(job.src, job.cache_dir, job.page, spec or self.render)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.id}>"


# ── text layer ────────────────────────────────────────────────────────────────

class TextLayerEngine(PageEngine):
    family = "textlayer"
    id = "textlayer"
    render = R.PRESETS["q120"]
    platforms = PLATFORMS_ALL + ("abap",)      # ZCL_MDMDOC_PDF is the ABAP equivalent

    def transcribe(self, job: PageJob) -> PageResult:
        import fitz
        t0 = time.time()
        if R.is_image(job.src):
            return PageResult("", [], "", 0.0, {"present": False}, None)
        with fitz.open(job.src) as d:
            pg = d[job.page]
            text = pg.get_text("text", sort=True) or ""
            blocks = pg.get_text("blocks") or []
        lines = []
        for b in sorted(blocks, key=lambda b: (round(b[1], 1), b[0])):
            if len(b) >= 5 and isinstance(b[4], str):
                for ln in b[4].split("\n"):
                    if ln.strip():
                        lines.append({"text": ln.strip(), "bbox": [b[0], b[1], b[2] - b[0], b[3] - b[1]]})
        usable, why = layer_usable(text)
        meta = {"present": bool(text.strip()), "usable": usable, "reason": why,
                "plausibility": plausibility(text) if text.strip() else None}
        return PageResult(text, lines, text, round(time.time() - t0, 3), meta, None)


# ── tesseract ─────────────────────────────────────────────────────────────────

def _tesseract(png: Path, lang: str, psm: int, timeout: int = 120) -> tuple[str, list[dict]]:
    """Text + TSV lines in one go (two cheap calls); cwd trick as in ocr.tesseract_text."""
    p = Path(png)
    base = ["tesseract", p.name, "-", "-l", lang, "--psm", str(psm)]
    r = subprocess.run(base, capture_output=True, timeout=timeout, cwd=str(p.parent))
    text = r.stdout.decode("utf-8", "replace")
    lines: list[dict] = []
    try:
        r2 = subprocess.run(base + ["tsv"], capture_output=True, timeout=timeout, cwd=str(p.parent))
        cur_key, cur_words, cur_conf, cur_box = None, [], [], None
        for row in r2.stdout.decode("utf-8", "replace").splitlines()[1:]:
            f = row.split("\t")
            if len(f) < 12 or f[0] != "5":
                continue
            key = (f[1], f[2], f[3], f[4])
            word, conf = f[11], float(f[10]) if f[10] not in ("", "-1") else 0.0
            if not word.strip():
                continue
            if key != cur_key and cur_words:
                lines.append({"text": " ".join(cur_words), "conf": round(sum(cur_conf) / len(cur_conf), 1),
                              "bbox": cur_box})
                cur_words, cur_conf = [], []
            cur_key = key
            cur_words.append(word)
            cur_conf.append(conf)
            cur_box = [int(f[6]), int(f[7]), int(f[8]), int(f[9])]
        if cur_words:
            lines.append({"text": " ".join(cur_words), "conf": round(sum(cur_conf) / len(cur_conf), 1),
                          "bbox": cur_box})
    except Exception:
        pass
    return text, lines


class TesseractEngine(PageEngine):
    family = "tess"
    render = R.PRESETS["g300"]
    platforms = PLATFORMS_ALL                  # a binary — not reachable from ABAP

    def __init__(self, lang: str = "auto", psm: int = 3, render_spec: R.RenderSpec | None = None):
        self.lang = lang or "auto"
        self.psm = psm
        if render_spec:
            self.render = render_spec
        mods = f"~psm{psm}" if psm != 3 else ""
        rend = f"@{self.render.name}" if self.render.name != "g300" else ""
        self.id = f"tess:{self.lang}{rend}{mods}"
        self.version = f"{CODE_VERSION}-tess{_tess_version()}"

    def available(self) -> tuple[bool, str]:
        if not ocr.HAVE_TESSERACT:
            return False, "tesseract not installed"
        if self.lang not in ("auto",):
            have = set(ocr._avail_langs()) if hasattr(ocr, "_avail_langs") else set()
            missing = [l for l in self.lang.split("+") if have and l not in have]
            if missing:
                return False, f"tesseract language pack(s) missing: {','.join(missing)}"
        return True, ""

    def transcribe(self, job: PageJob) -> PageResult:
        png = self.page_image(job)
        t0 = time.time()
        if self.lang == "auto":
            from ..stage_a import _quick_ocr
            text = _quick_ocr(str(png), force_cjk=True)
            lines = None
            if self.psm != 3:
                text, lines = _tesseract(png, "eng", self.psm)
        else:
            text, lines = _tesseract(png, self.lang, self.psm, timeout=job.timeout_s or 120)
            if ocr.cjk_char_count(text) >= 4:
                text = ocr.collapse_cjk_spaces(text)
        return PageResult(text, lines, None, round(time.time() - t0, 3),
                          {"lang": self.lang, "psm": self.psm}, None)


def _tess_version() -> str:
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True, timeout=10).stdout.decode()
        m = re.search(r"tesseract\s+v?([\d.]+)", out)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


# ── RapidOCR (PaddleOCR models on ONNX Runtime; CPU; offline) ────────────────

# Which recognition models to try for a script hint (manifest `scripts`) and the
# fallback order when there is no hint. PP-OCRv5/v6 "ch" covers Chinese, Japanese
# kana and English; "latin" has the accented letters of de/fr/es/pl/hu that the
# Chinese dictionary lacks; the others are single-script dictionaries.
RAPIDOCR_BY_SCRIPT = {
    "Han": ["ch"], "Kana": ["ch", "japan"], "Hangul": ["korean"], "Cyrillic": ["cyrillic"],
    "Arabic": ["arabic"], "Latin": ["latin", "en"],
}
RAPIDOCR_FALLBACK = ["latin", "ch"]
RAPIDOCR_MIN_CONF = 0.85          # mean line confidence below this → try the next model


class RapidOCREngine(PageEngine):
    """Second, independent OCR voice for the consensus layer: different models,
    different training data and a different text detector than tesseract, so the
    two do not share failure modes. Runs on CPU everywhere (Windows included) and
    never touches the network once its model files are present."""
    family = "rapidocr"
    render = R.PRESETS["v200"]
    platforms = PLATFORMS_ALL

    def __init__(self, lang: str = "auto", render_spec: R.RenderSpec | None = None):
        self.lang = lang or "auto"
        if render_spec:
            self.render = render_spec
        rend = f"@{self.render.name}" if self.render.name != "v200" else ""
        self.id = f"rapidocr:{self.lang}{rend}"
        self.version = f"{CODE_VERSION}-rapidocr{_rapidocr_version()}"
        self._engines: dict[str, object] = {}

    def available(self) -> tuple[bool, str]:
        try:
            import rapidocr  # noqa: F401
            import onnxruntime  # noqa: F401
        except ImportError as e:
            return False, f"rapidocr/onnxruntime not installed ({e}); uv sync --group bench"
        return True, ""

    def _engine(self, lang: str):
        eng = self._engines.get(lang)
        if eng is None:
            import logging
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            from rapidocr import RapidOCR
            from rapidocr.utils.typings import LangRec, ModelType, OCRVersion
            params = {"Rec.lang_type": LangRec(lang)}
            if lang not in ("ch", "ch_doc"):
                # the per-script dictionaries ship as PP-OCRv5 "mobile" models (japan: v4);
                # v6 is Chinese-only so far
                params["Rec.ocr_version"] = OCRVersion("PP-OCRv4" if lang == "japan" else "PP-OCRv5")
                params["Rec.model_type"] = ModelType("mobile")
            eng = RapidOCR(params=params)
            self._engines[lang] = eng
        return eng

    def _read(self, png: Path, lang: str) -> tuple[str, list[dict], float]:
        r = self._engine(lang)(str(png))
        txts = list(r.txts or []) if r is not None else []
        scores = list(r.scores or []) if r is not None else []
        boxes = r.boxes if r is not None else None
        lines = []
        for i, t in enumerate(txts):
            box = None
            if boxes is not None and i < len(boxes):
                b = boxes[i]
                xs = [float(pt[0]) for pt in b]
                ys = [float(pt[1]) for pt in b]
                box = [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]
            lines.append({"text": t, "conf": round(float(scores[i]) * 100, 1) if i < len(scores) else None,
                          "bbox": box})
        text = "\n".join(_rapidocr_reading_order(lines))
        mean = sum(float(x) for x in scores) / len(scores) if scores else 0.0
        return text, lines, mean

    def _candidates(self, hints: dict) -> list[str]:
        if self.lang != "auto":
            return [self.lang]
        out: list[str] = []
        for sc in hints.get("scripts") or []:
            for l in RAPIDOCR_BY_SCRIPT.get(sc, []):
                if l not in out:
                    out.append(l)
        for l in RAPIDOCR_FALLBACK:
            if l not in out:
                out.append(l)
        return out

    def transcribe(self, job: PageJob) -> PageResult:
        png = self.page_image(job)
        t0 = time.time()
        best = None
        tried = []
        for lang in self._candidates(job.hints):
            text, lines, mean = self._read(png, lang)
            tried.append({"lang": lang, "lines": len(lines), "mean_conf": round(mean, 3)})
            if best is None or (mean, len(lines)) > (best[2], len(best[1])):
                best = (text, lines, mean, lang)
            if mean >= RAPIDOCR_MIN_CONF and lines:
                break
        text, lines, mean, lang = best if best else ("", [], 0.0, "")
        if ocr.cjk_char_count(text) >= 4:
            text = ocr.collapse_cjk_spaces(text)
        return PageResult(text, lines, None, round(time.time() - t0, 3),
                          {"lang": lang, "mean_conf": round(mean, 3), "tried": tried}, None)


def _rapidocr_reading_order(lines: list[dict]) -> list[str]:
    """Group detected boxes into visual rows (centre-y within half a box height),
    left to right inside a row; rows top to bottom. Cells of one row join with two
    spaces so table columns stay apart."""
    items = [(ln["bbox"], ln["text"]) for ln in lines if ln.get("bbox")]
    if len(items) != len(lines):
        return [ln["text"] for ln in lines]
    items.sort(key=lambda it: ((it[0][1] + it[0][3]) / 2, it[0][0]))
    rows: list[list] = []
    for box, text in items:
        cy, h = (box[1] + box[3]) / 2, max(1, box[3] - box[1])
        if rows and abs(rows[-1][0] - cy) <= 0.5 * h:
            rows[-1][1].append((box[0], text))
            n = len(rows[-1][1])
            rows[-1][0] = (rows[-1][0] * (n - 1) + cy) / n
        else:
            rows.append([cy, [(box[0], text)]])
    return ["  ".join(t for _, t in sorted(r[1])) for r in rows]


def _rapidocr_version() -> str:
    try:
        from importlib.metadata import version
        return version("rapidocr")
    except Exception:
        return "?"


# ── Apple Vision (Swift CLI) ──────────────────────────────────────────────────

def visionocr_binary() -> Path:
    return config.PROJECT_ROOT / "tools" / "visionocr" / "visionocr"


def ensure_visionocr() -> tuple[bool, str]:
    b = visionocr_binary()
    if b.exists():
        return True, ""
    src = b.with_name("main.swift")
    if not src.exists():
        return False, f"{src} missing"
    if not shutil.which("xcrun"):
        return False, "xcrun/swiftc not available (Xcode command line tools)"
    try:
        r = subprocess.run(["xcrun", "swiftc", "-O", "-o", str(b), str(src)],
                           capture_output=True, timeout=900, cwd=str(b.parent))
        if r.returncode != 0:
            return False, "swiftc failed: " + r.stderr.decode("utf-8", "replace")[-400:]
    except Exception as e:
        return False, f"swiftc: {e}"
    return b.exists(), ""


class AppleVisionEngine(PageEngine):
    family = "applevision"
    render = R.PRESETS["g300"]
    platforms = ("macos",)

    def __init__(self, mode: str = "legacy", langs: list[str] | None = None,
                 render_spec: R.RenderSpec | None = None, correct: bool = True):
        self.mode = mode if mode in ("legacy", "document") else "legacy"
        self.langs = langs or []
        self.correct = correct
        if render_spec:
            self.render = render_spec
        rend = f"@{self.render.name}" if self.render.name != "g300" else ""
        ls = f"~langs={'+'.join(self.langs)}" if self.langs else ""
        nc = "" if correct else "~nocorrect"
        self.id = f"applevision:{self.mode}{rend}{ls}{nc}"
        self.version = f"{CODE_VERSION}-{_file_sha8(visionocr_binary().with_name('main.swift'))}"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        ok, why = ensure_visionocr()
        if not ok:
            return False, why
        if self.mode == "document":
            try:
                out = subprocess.run([str(visionocr_binary()), "--info"], capture_output=True, timeout=30)
                info = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
                if not info.get("document_mode"):
                    return False, "RecognizeDocumentsRequest needs macOS 26+"
            except Exception as e:
                return False, f"visionocr --info failed: {e}"
        return True, ""

    def setup(self) -> None:
        args = [str(visionocr_binary()), "--mode", self.mode]
        if self.langs:
            args += ["--langs", ",".join(self.langs)]
        if not self.correct:
            args.append("--nocorrect")
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                      start_new_session=True)

    def teardown(self) -> None:
        p, self._proc = self._proc, None
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _ask(self, png: Path, timeout: int) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            self.setup()
        assert self._proc and self._proc.stdin and self._proc.stdout
        with self._lock:
            self._proc.stdin.write(str(png) + "\n")
            self._proc.stdin.flush()
            line: list[str] = []

            def reader():
                line.append(self._proc.stdout.readline())

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                try:
                    self._proc.kill()
                finally:
                    self._proc = None
                raise TimeoutError(f"visionocr timed out after {timeout}s")
        raw = line[0] if line else ""
        if not raw.strip():
            raise RuntimeError("visionocr returned nothing (process died?)")
        return json.loads(raw)

    def transcribe(self, job: PageJob) -> PageResult:
        png = self.page_image(job)
        t0 = time.time()
        out = self._ask(png, job.timeout_s or 120)
        text = out.get("text", "") or ""
        if ocr.cjk_char_count(text) >= 4:
            text = ocr.collapse_cjk_spaces(text)
        return PageResult(text, out.get("lines"), out.get("markdown") or None,
                          round(time.time() - t0, 3),
                          {"mode": self.mode, "ms": out.get("ms")}, out.get("error"))


def _file_sha8(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:8]
    except Exception:
        return "00000000"


# ── Ollama vision models ──────────────────────────────────────────────────────

# per-model-family defaults (prefix match on the model name)
PROFILES: dict[str, dict] = {
    "qwen2.5vl": {"num_ctx": 16384, "num_predict": 4096},
    # 24k, not 32k: the bench host (Mac mini, 16 GB) holds qwen3-vl:8b at 32k as
    # 10.6 GB resident and swaps 9.5 GB — 5.8 min wall-clock per page vs 49 s of
    # model time. HARD limit for that machine: <= 24k context (memory note
    # mini-model-memory-limit). 24k is still 4x the longest page we emit.
    "qwen3-vl": {"num_ctx": 24576, "num_predict": 6144},
    "deepseek-ocr": {"num_ctx": 8192, "num_predict": 3000, "prompt": "deepseek_ocr",
                     "max_side": 1280},
    "gemma3": {"num_ctx": 16384, "num_predict": 4096},
    "gemma4": {"num_ctx": 16384, "num_predict": 4096},
    "minicpm-v": {"num_ctx": 16384, "num_predict": 4096},
    "granite3.2-vision": {"num_ctx": 16384, "num_predict": 3000},
    "llama3.2-vision": {"num_ctx": 16384, "num_predict": 4096},
    "moondream": {"num_ctx": 2048, "num_predict": 1024},
    "_default": {"num_ctx": 16384, "num_predict": 4096},
}


def profile_for(model: str) -> dict:
    base = model.split(":")[0].lower()
    for k, v in PROFILES.items():
        if k != "_default" and base.startswith(k):
            return dict(v)
    return dict(PROFILES["_default"])


def merge_tile_texts(texts: list[str], cutoff: int = 90) -> str:
    """Concatenate tile transcripts in reading order, dropping lines that repeat
    (fuzzy) a line already emitted by an earlier tile — the overlap band."""
    from rapidfuzz import fuzz
    out: list[str] = []
    seen: list[str] = []
    for t in texts:
        for ln in (t or "").split("\n"):
            s = ln.strip()
            if not s:
                continue
            key = re.sub(r"\s+", " ", s).casefold()
            dup = any(fuzz.ratio(key, k) >= cutoff for k in seen[-40:]) if len(key) >= 6 else key in seen[-40:]
            if dup:
                continue
            out.append(s)
            seen.append(key)
    return "\n".join(out)


RETRY_REPEAT_PENALTY = 1.3
RETRY_LAYOUTS = ("h2", "q4")


def _recover_from_loop(text: str, truncated: bool, meta: dict, page: Path, used_layout: str | None,
                       read) -> str:
    """Policy shared by the VLM engines: a page that came back looped or cut off at the
    token limit is re-read as tiles (h2, then q4 — skipping a layout already used),
    `read(images) -> (text, truncated)`. The first re-read that is non-empty, not
    looped and not truncated wins. If none qualifies the last non-empty re-read is
    kept after collapse_repeats(). Invariant: once a re-read produced ANY text the
    original looped output is never returned."""
    looped, why = looks_looped(text)
    meta["loop_detected"] = looped
    meta["loop_reason"] = why
    meta["retry"] = []
    if not (looped or truncated):
        return text
    last_nonempty = ""
    for layout in RETRY_LAYOUTS:
        if layout == used_layout:
            continue
        imgs = R.tiles(page, layout)
        t2, tr2 = read(imgs)
        l2, why2 = looks_looped(t2)
        meta["retry"].append({"layout": layout, "tiles": len(imgs), "chars": len(t2),
                              "looped": l2, "truncated": tr2, "reason": why2})
        if t2.strip():
            last_nonempty = t2
            if not l2 and not tr2:
                meta["recovered"] = layout
                return t2
    if last_nonempty:
        meta["recovered"] = "collapsed"
        return collapse_repeats(last_nonempty)
    meta["recovered"] = "collapsed-original"
    return collapse_repeats(text)


class OllamaVLMEngine(PageEngine):
    family = "ollama"
    render = R.PRESETS["v170"]
    default_timeout_s = 300
    platforms = PLATFORMS_ALL + ("abap",)      # ZCL_MDMDOC_LLM talks to Ollama over HTTP

    def __init__(self, model: str, render_spec: R.RenderSpec | None = None, prompt: str | None = None,
                 tiles: str | None = None, ocrhint: bool = False, twopass: bool = False,
                 host: str | None = None, keep_alive: str = "45m"):
        from . import ollama as O
        self.O = O
        self.model = model
        self.prof = profile_for(model)
        self.render = render_spec or R.PRESETS["v170"]
        if self.prof.get("max_side") and (self.render.max_side or 10**9) > self.prof["max_side"]:
            self.render = R.with_name(self.render, max_side=self.prof["max_side"])
        self.prompt_name = prompt or self.prof.get("prompt") or "transcribe_md"
        self.prompt_text, self.prompt_tag, psha = resolve_prompt(self.prompt_name)
        self.tiles = tiles
        self.ocrhint = ocrhint
        self.twopass = twopass
        self.host = host
        self.keep_alive = keep_alive
        self.caps: list[str] = []
        mods = ""
        if tiles:
            mods += f"~tiles:{tiles}"
        if ocrhint:
            mods += "~ocrhint"
        if twopass:
            mods += "~twopass"
        self.id = f"ollama:{model}@{self.render.name}#{self.prompt_tag}{mods}"
        extra = ""
        if ocrhint:
            extra += "-" + resolve_prompt("ocrhint")[2]
        if twopass:
            extra += "-" + resolve_prompt("verify")[2]
        self.version = f"{CODE_VERSION}-{psha}{extra}-o{_opts_sha8(self._options())}"

    def available(self) -> tuple[bool, str]:
        if not self.O.alive(self.host):
            return False, f"ollama not reachable at {self.host or self.O.host()}"
        if self.model not in self.O.available_models(self.host):
            return False, f"model {self.model} not pulled (ollama pull {self.model})"
        if not self.O.has_vision(self.model, self.host):
            return False, f"model {self.model} has no vision capability"
        return True, ""

    def setup(self) -> None:
        try:
            info = self.O.show(self.model, self.host)
            self.caps = list(info.get("capabilities") or [])
        except Exception:
            self.caps = []
        for name in self.O.unload_all(self.host):
            if name != self.model:
                pass
        self.O.warm(self.model, keep_alive=self.keep_alive, h=self.host)

    def teardown(self) -> None:
        self.O.unload(self.model, self.host)

    def _options(self, repeat_penalty: float | None = None) -> dict:
        # repeat_penalty is load-bearing, not tuning: without it qwen2.5vl:7b fell into
        # a loop on a French RIB (one table cell repeated to the 4096-token limit, 279 s,
        # zero values extracted); with 1.15 the same page took 90 s and every key
        # value was present. 3-10% of wave-1 pages looped this way. The retry path
        # passes a stronger penalty explicitly.
        return {"temperature": 0, "seed": 7, "num_ctx": self.prof["num_ctx"],
                "num_predict": self.prof["num_predict"],
                "repeat_penalty": repeat_penalty if repeat_penalty is not None
                else self.prof.get("repeat_penalty", 1.15)}

    def _call(self, prompt: str, images: list[Path], timeout: int,
              repeat_penalty: float | None = None) -> tuple[str, dict]:
        think = False if "thinking" in self.caps else None
        text, stats = self.O.generate(self.model, prompt, images, options=self._options(repeat_penalty),
                                      keep_alive=self.keep_alive, timeout=timeout, think=think,
                                      h=self.host)
        return _strip_fences(text), stats

    def _read(self, prompt: str, images: list[Path], timeout: int, meta: dict, *,
              repeat_penalty: float | None = None) -> tuple[str, bool]:
        """Transcribe one image list (a page, or its tiles) → (merged text, truncated?)."""
        texts = []
        truncated = False
        for img in images:
            text, stats = self._call(prompt, [img], timeout, repeat_penalty)
            meta["calls"] += 1
            truncated = truncated or stats.get("done_reason") == "length"
            meta["eval_count"] = meta.get("eval_count", 0) + (stats.get("eval_count") or 0)
            texts.append(text)
        return (merge_tile_texts(texts) if len(texts) > 1 else (texts[0] if texts else "")), truncated

    def transcribe(self, job: PageJob) -> PageResult:
        page = self.page_image(job)
        timeout = job.timeout_s or self.default_timeout_s
        t0 = time.time()
        meta: dict = {"model": self.model, "prompt": self.prompt_tag, "render": self.render.name,
                      "calls": 0, "truncated": False}
        prompt = self.prompt_text
        if self.ocrhint:
            hint = job.hints.get("ocr_text")
            if hint is None:
                from ..stage_a import _quick_ocr
                g = R.render_page(job.src, job.cache_dir, job.page, R.PRESETS["g300"])
                hint = _quick_ocr(str(g), force_cjk=True)
            prompt = resolve_prompt("ocrhint")[0].replace("{ocr_text}", hint[:6000])
            meta["ocrhint_chars"] = len(hint)
        images = [page]
        if self.tiles:
            images = R.tiles(page, self.tiles)
            meta["tiles"] = len(images)
        text, truncated = self._read(prompt, images, timeout, meta)
        meta["truncated"] = truncated
        text = _recover_from_loop(text, truncated, meta, page, self.tiles,
                                  lambda imgs: self._read(prompt, imgs, timeout, meta,
                                                          repeat_penalty=RETRY_REPEAT_PENALTY))
        if self.twopass and text.strip():
            vprompt = resolve_prompt("verify")[0].replace("{draft}", text[:12000])
            text2, stats2 = self._call(vprompt, [page], timeout)
            meta["calls"] += 1
            if text2.strip():
                meta["draft_chars"] = len(text)
                text = text2
        return PageResult(text, None, text, round(time.time() - t0, 2), meta, None)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.fullmatch(r"```(?:markdown|md|text)?\s*(.*?)\s*```", t, re.DOTALL)
    return m.group(1).strip() if m else t


# ── MLX (tools/mlxvlm worker) ─────────────────────────────────────────────────

class MLXVLMEngine(PageEngine):
    family = "mlx"
    render = R.PRESETS["v200"]
    default_timeout_s = 300
    platforms = ("macos",)                     # Apple Silicon only

    def __init__(self, repo: str, render_spec: R.RenderSpec | None = None, prompt: str | None = None,
                 tiles: str | None = None, ocrhint: bool = False, twopass: bool = False):
        self.repo = repo
        self.render = render_spec or R.PRESETS["v200"]
        short = repo.split("/")[-1]
        self.prof = mlx_profile_for(short)
        if self.prof.get("max_side") and (self.render.max_side or 10**9) > self.prof["max_side"]:
            self.render = R.with_name(self.render, max_side=self.prof["max_side"])
        self.prompt_name = prompt or self.prof.get("prompt") or "transcribe_md"
        self.prompt_text, self.prompt_tag, psha = resolve_prompt(self.prompt_name)
        self.tiles = tiles
        self.ocrhint = ocrhint
        self.twopass = twopass
        mods = (f"~tiles:{tiles}" if tiles else "") + ("~ocrhint" if ocrhint else "") + ("~twopass" if twopass else "")
        self.id = f"mlx:{short}@{self.render.name}#{self.prompt_tag}{mods}"
        self.version = f"{CODE_VERSION}-{psha}-o{_opts_sha8(self._gen_options())}"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._restarts = 0

    @staticmethod
    def worker_dir() -> Path:
        return config.PROJECT_ROOT / "tools" / "mlxvlm"

    def available(self) -> tuple[bool, str]:
        wd = self.worker_dir()
        if not (wd / "worker.py").exists():
            return False, "tools/mlxvlm/worker.py missing"
        if not shutil.which("uv"):
            return False, "uv not found"
        if not (wd / ".venv").exists():
            return False, "tools/mlxvlm not synced (uv sync --project tools/mlxvlm)"
        # weights cached?
        try:
            r = subprocess.run(["uv", "run", "--project", str(wd), "python", "worker.py",
                                "--model", self.repo, "--check"],
                               capture_output=True, timeout=120, cwd=str(wd))
            if r.returncode != 0:
                return False, (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace"))[-300:]
        except Exception as e:
            return False, f"worker check failed: {e}"
        return True, ""

    def setup(self) -> None:
        wd = self.worker_dir()
        self._proc = subprocess.Popen(
            ["uv", "run", "--project", str(wd), "python", "worker.py", "--model", self.repo],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.devnull, "wb"),
            text=True, bufsize=1, cwd=str(wd), start_new_session=True)
        # wait for the ready line
        ready = self._readline(600)
        if not ready or '"ready"' not in ready:
            raise EngineUnavailable(f"mlx worker did not become ready: {ready!r}")

    def teardown(self) -> None:
        p, self._proc = self._proc, None
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=15)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), 9)
            except Exception:
                pass

    def _readline(self, timeout: int) -> str:
        assert self._proc and self._proc.stdout
        box: list[str] = []

        def reader():
            box.append(self._proc.stdout.readline())

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            try:
                os.killpg(os.getpgid(self._proc.pid), 9)
            finally:
                self._proc = None
            raise TimeoutError(f"mlx worker timed out after {timeout}s")
        return box[0] if box else ""

    def _gen_options(self) -> dict:
        return {"max_tokens": self.prof.get("num_predict", 4096)}

    def _call(self, prompt: str, images: list[Path], timeout: int) -> tuple[str, dict]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if self._restarts >= 3:
                    raise EngineUnavailable("mlx worker crashed repeatedly")
                self._restarts += 1
                self.setup()
            assert self._proc and self._proc.stdin
            req = {"images": [str(p) for p in images], "prompt": prompt, **self._gen_options()}
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            raw = self._readline(timeout)
        if not raw.strip():
            raise RuntimeError("mlx worker returned nothing")
        out = json.loads(raw)
        if out.get("error"):
            raise RuntimeError(out["error"])
        return _strip_fences(out.get("text", "")), out

    def _read(self, prompt: str, images: list[Path], timeout: int, meta: dict) -> tuple[str, bool]:
        texts = []
        truncated = False
        for img in images:
            text, stats = self._call(prompt, [img], timeout)
            meta["calls"] += 1
            truncated = truncated or bool(stats.get("truncated"))
            meta["peak_mem_gb"] = stats.get("peak_mem_gb")
            meta["gen_tokens"] = meta.get("gen_tokens", 0) + (stats.get("gen_tokens") or 0)
            texts.append(text)
        return (merge_tile_texts(texts) if len(texts) > 1 else (texts[0] if texts else "")), truncated

    def transcribe(self, job: PageJob) -> PageResult:
        page = self.page_image(job)
        timeout = job.timeout_s or self.default_timeout_s
        t0 = time.time()
        meta: dict = {"model": self.repo, "prompt": self.prompt_tag, "render": self.render.name,
                      "calls": 0, "truncated": False}
        prompt = self.prompt_text
        if self.ocrhint:
            hint = job.hints.get("ocr_text")
            if hint is None:
                from ..stage_a import _quick_ocr
                g = R.render_page(job.src, job.cache_dir, job.page, R.PRESETS["g300"])
                hint = _quick_ocr(str(g), force_cjk=True)
            prompt = resolve_prompt("ocrhint")[0].replace("{ocr_text}", hint[:6000])
        images = R.tiles(page, self.tiles) if self.tiles else [page]
        if self.tiles:
            meta["tiles"] = len(images)
        text, truncated = self._read(prompt, images, timeout, meta)
        meta["truncated"] = truncated
        # the worker has no repetition penalty yet — recovery is tiles only
        text = _recover_from_loop(text, truncated, meta, page, self.tiles,
                                  lambda imgs: self._read(prompt, imgs, timeout, meta))
        if self.twopass and text.strip():
            vprompt = resolve_prompt("verify")[0].replace("{draft}", text[:12000])
            text2, _ = self._call(vprompt, [page], timeout)
            meta["calls"] += 1
            if text2.strip():
                text = text2
        return PageResult(text, None, text, round(time.time() - t0, 2), meta, None)


MLX_PROFILES: dict[str, dict] = {
    "deepseek-ocr": {"prompt": "deepseek_ocr", "num_predict": 3000, "max_side": 1280},
    "olmocr": {"prompt": "olmocr", "num_predict": 4096},
    "nanonets-ocr": {"prompt": "nanonets_ocr", "num_predict": 4096},
    "dots.ocr": {"prompt": "dots_ocr", "num_predict": 4096},
    "paddleocr-vl": {"prompt": "paddleocr_vl", "num_predict": 4096},
    "_default": {"num_predict": 4096},
}


def mlx_profile_for(short: str) -> dict:
    s = short.lower()
    for k, v in MLX_PROFILES.items():
        if k != "_default" and s.startswith(k):
            return dict(v)
    return dict(MLX_PROFILES["_default"])


# ── spec parsing ──────────────────────────────────────────────────────────────

def parse(spec: str, **kw) -> PageEngine:
    """Build an engine from its spec string (see module docstring)."""
    s = spec.strip()
    if not s:
        raise ValueError("empty engine spec")
    # split modifiers
    parts = s.split("~")
    head, mods = parts[0], parts[1:]
    prompt = None
    if "#" in head:
        head, prompt = head.split("#", 1)
    render_name = None
    if "@" in head:
        head, render_name = head.split("@", 1)
    if ":" in head:
        family, model = head.split(":", 1)
    else:
        family, model = head, ""
    family = family.lower()
    rspec = R.preset(render_name) if render_name else None
    tiles = None
    ocrhint = twopass = False
    psm = 3
    langs: list[str] = []
    correct = True
    for m in mods:
        if m.startswith("tiles:"):
            tiles = m.split(":", 1)[1]
            if tiles not in R.TILE_LAYOUTS:
                raise ValueError(f"unknown tile layout {tiles!r}")
        elif m == "ocrhint":
            ocrhint = True
        elif m == "twopass":
            twopass = True
        elif re.fullmatch(r"psm\d+", m):
            psm = int(m[3:])
        elif m.startswith("langs="):
            langs = m.split("=", 1)[1].split("+")
        elif m == "nocorrect":
            correct = False
        else:
            raise ValueError(f"unknown modifier {m!r} in {spec!r}")
    if family == "textlayer":
        return TextLayerEngine()
    if family == "tess":
        return TesseractEngine(model or "auto", psm=psm, render_spec=rspec)
    if family == "rapidocr":
        return RapidOCREngine(model or "auto", render_spec=rspec)
    if family == "applevision":
        return AppleVisionEngine(model or "legacy", langs=langs, render_spec=rspec, correct=correct)
    if family == "ollama":
        if not model:
            raise ValueError("ollama engine needs a model: ollama:<model>")
        return OllamaVLMEngine(model, render_spec=rspec, prompt=prompt, tiles=tiles,
                               ocrhint=ocrhint, twopass=twopass, host=kw.get("host"))
    if family == "mlx":
        if not model:
            raise ValueError("mlx engine needs a repo: mlx:<hf-repo>")
        return MLXVLMEngine(model, render_spec=rspec, prompt=prompt, tiles=tiles,
                            ocrhint=ocrhint, twopass=twopass)
    raise ValueError(f"unknown engine family {family!r} in {spec!r}")


def parse_many(specs: str | list[str], **kw) -> list[PageEngine]:
    items = specs if isinstance(specs, list) else [x for x in specs.split(",") if x.strip()]
    return [parse(x, **kw) for x in items]


def safe_id(engine_id: str) -> str:
    """Filesystem-safe name for an engine id."""
    return re.sub(r"[^A-Za-z0-9._=+-]+", "_", engine_id).strip("_")[:120]


def platforms_of(engine_id: str) -> tuple[str, ...]:
    """Platforms an engine id can run on — works for the virtual layer>/layer+ ids too."""
    eid = engine_id
    for prefix in ("layer>", "layer+"):
        if eid.startswith(prefix):
            eid = eid[len(prefix):]
    family = eid.split(":", 1)[0].split("@", 1)[0]
    table = {"textlayer": TextLayerEngine, "tess": TesseractEngine,
             "applevision": AppleVisionEngine, "ollama": OllamaVLMEngine, "mlx": MLXVLMEngine,
             "rapidocr": RapidOCREngine}
    cls = table.get(family)
    plats = tuple(getattr(cls, "platforms", PLATFORMS_ALL)) if cls else PLATFORMS_ALL
    if engine_id.startswith(("layer>", "layer+")):
        # the merge policy needs a text layer as well — both parts must be available
        plats = tuple(p for p in plats if p in TextLayerEngine.platforms)
    return plats


def runs_on(engine_id: str, platform: str) -> bool:
    return platform in platforms_of(engine_id)
