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
    in run artifacts and the UI.
    Explicit MDMDOC_BANK_VALUES wins; else operator console -> full, BTP -> masked."""
    v = os.environ.get("MDMDOC_BANK_VALUES", "").strip().lower()
    if v in ("full", "masked"):
        return v
    return "masked" if os.environ.get("MDMDOC_MODE", "full") == "api-only" else "full"


def tin_values_policy() -> str:
    """'full' | 'masked' — how TAX numbers (TIN/SSN/EIN, W-8 foreign/US TIN)
    appear in run artifacts and the UI. Same shape as bank_values_policy: the
    operator types these into SAP and the source document is one click away, so
    the console shows them in full; BTP/api-only stays masked.

    This governs the OPERATOR display policy only. Three things it can never
    reach, by construction: training data (dataset/few-shot/LoRA gates are
    always strict), outbound web verification (egress.py forbids TIN_KINDS
    unconditionally), and any caller that asks for policy='masked' explicitly."""
    v = os.environ.get("MDMDOC_TIN_VALUES", "").strip().lower()
    if v in ("full", "masked"):
        return v
    return "masked" if os.environ.get("MDMDOC_MODE", "full") == "api-only" else "full"


def gate_policy() -> str:
    """Leak-gate mode for RUN ARTIFACTS — it blocks exactly the value families the
    display policy still masks, so a family shown in full can never trip the gate
    on its own legitimate content:

        bank masked -> 'strict'   (both families blocked; tax numbers are masked
                                   too, because a surface that hides banking gets
                                   policy='masked' and privacy.tin_visible is False)
        bank full, tin masked -> 'tin-only'
        bank full, tin full   -> 'none'

    'none' is not a disabled gate: it is the honest statement that this artifact
    is allowed to carry both. Training data is ALWAYS gated strict."""
    if bank_values_policy() != "full":
        return "strict"
    return "none" if tin_values_policy() == "full" else "tin-only"

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


# --- effort slider (D4) ---------------------------------------------------------
# The ONE user-facing depth control: 1 = instant no-LLM, 3 = today's auto
# (byte-identical), 5 = maximum scrutiny. Each level resolves to an engine, the
# strong-tier flag and per-run knob overrides carried via runctl (thread-local,
# never env). Replaces the thorough checkbox + per-run engine select in the UI.
EFFORT_DEFAULT = 3

_EFFORT_PROFILES = {
    1: {"engine": "deterministic", "quality": False,
        "overrides": {"ladder_enabled": False}},
    2: {"engine": "auto", "quality": False,
        "overrides": {"strong_enabled": False, "ladder_vision_cap": 0}},
    3: {"engine": "auto", "quality": False, "overrides": {}},
    4: {"engine": "auto", "quality": True, "overrides": {"ladder_pages": 3}},
    5: {"engine": "llm-first", "quality": True,
        "overrides": {"sig_vision_cap": 6, "ladder_vision_cap": 2,
                      "ladder_pages": 3, "time_budget_s": 420,
                      "strong_model_options": {"num_predict": 4096},
                      "strong_think": True}},
}


def effort_profile(level: int) -> dict:
    """-> {'engine', 'quality', 'overrides'} for a clamped 1..5 level."""
    lvl = min(5, max(1, int(level)))
    p = _EFFORT_PROFILES[lvl]
    return {"engine": p["engine"], "quality": p["quality"],
            "overrides": dict(p["overrides"])}


def default_effort() -> int:
    """Operator-panel default for the slider (settings.json), clamped."""
    try:
        return min(5, max(1, int(load_settings().get("default_effort",
                                                     EFFORT_DEFAULT))))
    except (TypeError, ValueError):
        return EFFORT_DEFAULT


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
