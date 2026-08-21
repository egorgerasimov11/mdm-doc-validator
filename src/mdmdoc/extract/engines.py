"""Page engines — the pluggable transcription layer shared by the benchmark
and the extractor.

An engine turns ONE rendered page into text (PageResult). Engines are built
from a spec string:

    family:model[@render][#prompt][~mod…]

    textlayer                         PDF text layer (PyMuPDF, reading order)
    tess:auto | tess:kor+eng~psm6     tesseract (auto = production CJK-retry logic)
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
from .plausibility import layer_usable, plausibility

CODE_VERSION = "1"


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
    "qwen3-vl": {"num_ctx": 32768, "num_predict": 6144},
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
        self.version = f"{CODE_VERSION}-{psha}{extra}"

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

    def _options(self) -> dict:
        return {"temperature": 0, "seed": 7, "num_ctx": self.prof["num_ctx"],
                "num_predict": self.prof["num_predict"]}

    def _call(self, prompt: str, images: list[Path], timeout: int) -> tuple[str, dict]:
        think = False if "thinking" in self.caps else None
        text, stats = self.O.generate(self.model, prompt, images, options=self._options(),
                                      keep_alive=self.keep_alive, timeout=timeout, think=think,
                                      h=self.host)
        return _strip_fences(text), stats

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
        texts = []
        for img in images:
            text, stats = self._call(prompt, [img], timeout)
            meta["calls"] += 1
            meta["truncated"] = meta["truncated"] or stats.get("done_reason") == "length"
            meta.setdefault("eval_count", 0)
            meta["eval_count"] += stats.get("eval_count") or 0
            texts.append(text)
        text = merge_tile_texts(texts) if len(texts) > 1 else (texts[0] if texts else "")
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
        self.version = f"{CODE_VERSION}-{psha}"
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

    def _call(self, prompt: str, images: list[Path], timeout: int) -> tuple[str, dict]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if self._restarts >= 3:
                    raise EngineUnavailable("mlx worker crashed repeatedly")
                self._restarts += 1
                self.setup()
            assert self._proc and self._proc.stdin
            req = {"images": [str(p) for p in images], "prompt": prompt,
                   "max_tokens": self.prof.get("num_predict", 4096)}
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            raw = self._readline(timeout)
        if not raw.strip():
            raise RuntimeError("mlx worker returned nothing")
        out = json.loads(raw)
        if out.get("error"):
            raise RuntimeError(out["error"])
        return _strip_fences(out.get("text", "")), out

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
        texts = []
        for img in images:
            text, stats = self._call(prompt, [img], timeout)
            meta["calls"] += 1
            meta["truncated"] = meta["truncated"] or bool(stats.get("truncated"))
            meta["peak_mem_gb"] = stats.get("peak_mem_gb")
            meta.setdefault("gen_tokens", 0)
            meta["gen_tokens"] += stats.get("gen_tokens") or 0
            texts.append(text)
        text = merge_tile_texts(texts) if len(texts) > 1 else (texts[0] if texts else "")
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
             "applevision": AppleVisionEngine, "ollama": OllamaVLMEngine, "mlx": MLXVLMEngine}
    cls = table.get(family)
    plats = tuple(getattr(cls, "platforms", PLATFORMS_ALL)) if cls else PLATFORMS_ALL
    if engine_id.startswith(("layer>", "layer+")):
        # the merge policy needs a text layer as well — both parts must be available
        plats = tuple(p for p in plats if p in TextLayerEngine.platforms)
    return plats


def runs_on(engine_id: str, platform: str) -> bool:
    return platform in platforms_of(engine_id)
