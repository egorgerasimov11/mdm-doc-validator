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
LORA_DIR = DATASET_DIR / "mlx-lora"
EVAL_DIR = PROJECT_ROOT / "eval"
INBOX_DIR = PROJECT_ROOT / "inbox"      # uploaded documents (raw, gitignored)

SERVER_DEFAULT_PORT = 8766   # 8765 is Anki's local port on this Mac

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
