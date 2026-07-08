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


def ensure_dirs() -> None:
    for d in (RUNS_DIR, DATASET_DIR, LORA_DIR, EVAL_DIR, FEWSHOT_DIR, MODELS_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)
