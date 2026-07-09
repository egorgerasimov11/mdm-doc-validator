#!/usr/bin/env python3
"""rules_io.py — the rule-file write choke point.

Rules (rules/*.yaml) carry NO PII, so the privacy leak gate does not apply here;
but per the source-write guard (test_no_writes_outside_choke_points) all file
writes live in named modules, so rule editing from the console goes through here.
The model never decides verdicts — rules stay explicit and editable (invariant #1).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import yaml

from . import config

# serializes rule-file writes across the server threadpool
_LOCK = threading.Lock()


def rules_path(doc_class: str) -> Path:
    return config.RULES_DIR / ("banking.yaml" if doc_class == "bank" else "w9.yaml")


def save_rules(doc_class: str, text: str) -> int:
    """Validate and overwrite a doc-class rule file; return the rule count.
    A rule is 'deleted' by removing its block. Raises ValueError on bad input."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error: {e}")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list):
        raise ValueError("top level must be a mapping with a 'rules:' list")
    ids = [r.get("id") for r in parsed["rules"] if isinstance(r, dict)]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule id in the file")
    with _LOCK:
        config.atomic_write_text(rules_path(doc_class), text)
    return len(parsed["rules"])


def regenerate_abap() -> dict:
    """Push the edited YAML rules to the ABAP/SAP side: copy them into the ABAP
    repo and run its generator. Best-effort — reports if the repo is absent."""
    abap = Path(os.environ.get("MDMDOC_ABAP_HOME",
                               str(Path.home() / "Projects" / "mdm-doc-validator-abap")))
    gen = abap / "tools" / "gen_rules_abap.py"
    if not gen.exists():
        return {"ok": False, "detail": f"ABAP repo not found at {abap} "
                "(set MDMDOC_ABAP_HOME to enable SAP regeneration)"}
    try:
        (abap / "rules").mkdir(parents=True, exist_ok=True)
        for f in ("banking.yaml", "w9.yaml"):
            shutil.copy(config.RULES_DIR / f, abap / "rules" / f)
        r = subprocess.run(["python3", str(gen)], cwd=str(abap),
                           capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        return {"ok": False, "detail": str(e)}
    return {"ok": r.returncode == 0, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
