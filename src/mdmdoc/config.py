"""Paths and settings. Everything lives under the project root (editable install)."""
from __future__ import annotations

import os
from pathlib import Path

# src/mdmdoc/config.py -> project root (uv installs the project editable)
PROJECT_ROOT = Path(os.environ.get("MDMDOC_HOME", Path(__file__).resolve().parents[2]))

RULES_DIR = PROJECT_ROOT / "rules"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
FEWSHOT_DIR = PROMPTS_DIR / "fewshot"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
MODELS_DIR = PROJECT_ROOT / "models"
RUNS_DIR = PROJECT_ROOT / "runs"
DATASET_DIR = PROJECT_ROOT / "dataset"
LABELS_PATH = DATASET_DIR / "labels.jsonl"
# Labeled ORIGINAL documents live here (gitignored — they carry real PII).
# labels.jsonl stores doc_path RELATIVE to this dir, so the corpus + labels
# pair rsyncs to any machine (tools/migrate_corpus.py converts legacy rows).
CORPUS_DIR = Path(os.environ.get("MDMDOC_CORPUS_DIR", str(DATASET_DIR / "corpus")))
LORA_DIR = DATASET_DIR / "mlx-lora"
EVAL_DIR = PROJECT_ROOT / "eval"
INBOX_DIR = PROJECT_ROOT / "inbox"      # uploaded documents (raw, gitignored)
# Synthetic eval stratum (П1): PII-free generated corpus, COMMITTED for
# reproducibility. Never mixed into the headline real-corpus metrics; never
# visible to load_labels() (few-shot/LoRA/precedents can't train on it).
SYNTH_DIR = EVAL_DIR / "synthetic"
SYNTH_DOCS_DIR = SYNTH_DIR / "docs"
SYNTH_LABELS_PATH = SYNTH_DIR / "labels.jsonl"

SERVER_DEFAULT_PORT = 8766   # 8765 is Anki's local port on this Mac

# duration estimates shown to the user (seconds); refined by rolling means of
# past runs with the same shape (see estimate.py)
TIME_BASE_S = {"text_layer": 20, "scan": 90}
TIME_MODIFIERS_S = {"strong": 60, "signature": 30, "sap": 30}


def bank_values_policy() -> str:
    """'full' | 'masked' — how BANKING identifiers (account/routing/IBAN) appear
    in run artifacts and the UI. TIN/SSN/EIN is masked under EVERY policy.
    Explicit MDMDOC_BANK_VALUES wins; else operator console -> full, BTP -> masked."""
    v = os.environ.get("MDMDOC_BANK_VALUES", "").strip().lower()
    if v in ("full", "masked"):
        return v
    return "masked" if os.environ.get("MDMDOC_MODE", "full") == "api-only" else "full"


def gate_policy() -> str:
    """Leak-gate mode for run artifacts: 'tin-only' when banking values are shown
    in full, 'strict' otherwise. Training data is ALWAYS gated strict."""
    return "tin-only" if bank_values_policy() == "full" else "strict"

# Stage B input budget: OCR text is truncated to this many chars before the model sees it.
STAGE_B_TEXT_LIMIT = 8000
# Excerpt stored in labels / used for few-shot and LoRA examples.
EXCERPT_LIMIT = 1600

# vision render settings (mirrors form-validator: 170 DPI color for the model,
# 300 DPI grayscale for tesseract inside ocr.py)
VISION_DPI = 170
VISION_MAX_SIDE = 1600
MAX_PAGES = {"bank": 2, "w9": 3}

EXIT_ACCEPT = 0
EXIT_REJECT = 1
EXIT_REVIEW = 2          # WARNING / NEED_MANUAL_REVIEW
EXIT_OLLAMA_DOWN = 3
EXIT_UNREADABLE = 4

EDITABLE_EXTS = {".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".txt", ".rtf", ".csv", ".odt"}
EMAIL_EXTS = {".eml", ".msg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}

# --- analysis engine -----------------------------------------------------------
# Runtime operator settings (like rules/approvals.json: live state on the host,
# gitignored, never rsynced over). Today it holds one key: {"engine": "..."}.
SETTINGS_PATH = PROJECT_ROOT / "settings.json"

# auto          — deterministic + fast LLM, strong tier on weakness (default)
# deterministic — OCR + patterns + rules ONLY (no LLM, no vision)
# llm-first     — strong LLM tier from the start (== quality)
# dual          — auto + a per-field deterministic-vs-LLM comparison artifact
ENGINE_MODES = ("auto", "deterministic", "llm-first", "dual")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Crash-safe replace: write a sibling tmp file, then os.replace(). A reader
    sees the old bytes or the new bytes — never a torn/truncated file. Used by
    every operator-state writer (approvals ledger, labels, rules, settings)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def load_settings() -> dict:
    import json
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_setting(key: str, value) -> None:
    import json
    s = load_settings()
    s[key] = value
    atomic_write_text(SETTINGS_PATH,
                      json.dumps(s, ensure_ascii=False, indent=1) + "\n")


def _run_override(key: str):
    """Per-run override from the active RunControl (D1/effort): run > env >
    default. Lazy import — config must stay import-light (runctl imports none
    of config, so no cycle either way, but keep the seam cheap)."""
    from . import runctl
    return runctl.override(key)


def sig_vision_cap() -> int:
    """S2 signature-ensemble budget: every vision probe inside signature_probe
    counts against this cap. 2 == the pre-ensemble behavior (no escalation)."""
    ov = _run_override("sig_vision_cap")
    if ov is not None:
        return max(0, int(ov))
    try:
        return max(0, int(os.environ.get("MDMDOC_SIG_VISION_CAP", "4")))
    except ValueError:
        return 4


def _env_int(name: str, default: int, floor: int = 0) -> int:
    try:
        return max(floor, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def ladder_enabled() -> bool:
    """P6 evidence-ladder kill switch: MDMDOC_LADDER=0 disables the bounded
    second perception pass entirely. Default ON — clean documents never
    trigger it (no critical gaps -> zero extra cost)."""
    ov = _run_override("ladder_enabled")
    if ov is not None:
        return bool(ov)
    return os.environ.get("MDMDOC_LADDER", "1").strip() != "0"


def ladder_pages() -> int:
    """Max extra pages one ladder climb may deep-read."""
    ov = _run_override("ladder_pages")
    if ov is not None:
        return max(0, int(ov))
    return _env_int("MDMDOC_LADDER_PAGES", 2)


def ladder_vision_cap() -> int:
    """Max vision transcribe calls per ladder climb (0 = OCR-only)."""
    ov = _run_override("ladder_vision_cap")
    if ov is not None:
        return max(0, int(ov))
    return _env_int("MDMDOC_LADDER_VISION_CAP", 1)


def time_budget_s() -> float:
    """Whole-run soft budget: the ladder refuses to start past this point."""
    ov = _run_override("time_budget_s")
    if ov is not None:
        return float(ov)
    return float(_env_int("MDMDOC_TIME_BUDGET_S", 240))


def doctype_rescue_enabled() -> bool:
    """П3 evidence-rescue kill switch: MDMDOC_DOCTYPE_RESCUE=0 reverts to the
    blunt payment_instructions->other->NMR grounding. Default ON."""
    return os.environ.get("MDMDOC_DOCTYPE_RESCUE", "1").strip() != "0"


def engine_mode() -> str:
    """Which analysis engine drives a check when the request does not say:
    MDMDOC_ENGINE env (ops override) > settings.json (operator panel) > auto."""
    v = os.environ.get("MDMDOC_ENGINE", "").strip().lower()
    if v in ENGINE_MODES:
        return v
    v = str(load_settings().get("engine", "")).strip().lower()
    return v if v in ENGINE_MODES else "auto"


def ensure_dirs() -> None:
    for d in (RUNS_DIR, DATASET_DIR, LORA_DIR, EVAL_DIR, FEWSHOT_DIR, MODELS_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)
