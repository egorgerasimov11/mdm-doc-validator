#!/usr/bin/env python3
"""rules_io.py — the rule-file write choke point.

Rules (rules/*.yaml) carry NO PII, so the privacy leak gate does not apply here;
but per the source-write guard (test_no_writes_outside_choke_points) all file
writes live in named modules, so rule editing from the console goes through here.
The model never decides verdicts — rules stay explicit and editable (invariant #1).
"""
from __future__ import annotations

import os
import re
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
    A rule is 'deleted' by removing its block. Raises ValueError on bad input.
    Every rule passes the SEMANTIC validation too (unknown predicate, bad
    message placeholder, typo'd verdict_effect) — such a rule would fail
    closed at runtime (ENGINE-GUARD NMR), so refuse to save it at all."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error: {e}")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list):
        raise ValueError("top level must be a mapping with a 'rules:' list")
    ids = [r.get("id") for r in parsed["rules"] if isinstance(r, dict)]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule id in the file")
    from .rule_propose import validate_rule   # function-local: rule_propose imports rules_io
    known = set(parsed.get("doc_types") or [])
    problems: list[str] = []
    for r in parsed["rules"]:
        if not isinstance(r, dict):
            problems.append("a rules: entry is not a mapping")
            continue
        for issue in validate_rule(r, doc_class, known_doc_types=known):
            problems.append(f"rule {r.get('id', '?')}: {issue}")
    if problems:
        raise ValueError("; ".join(problems))
    with _LOCK:
        config.atomic_write_text(rules_path(doc_class), text)
    return len(parsed["rules"])


_TIERS = ("corp", "experimental", "learned")


def set_rule_tier(doc_class: str, rule_id: str, tier: str) -> dict:
    """П7 promotion apply: SURGICAL edit of only the `tier:` line inside the
    rule's block (a YAML re-dump would destroy in-block comments). `tier` is
    approval-hash-immune (rule_approvals._METADATA_KEYS), so this NEVER resets
    the rule to pending — the hard gate is untouched."""
    if tier not in _TIERS:
        raise ValueError(f"tier must be one of {_TIERS}, got {tier!r}")
    from .rule_propose import _rule_block_span
    p = rules_path(doc_class)
    text = p.read_text(encoding="utf-8")
    span = _rule_block_span(text, rule_id)
    if not span:
        raise ValueError(f"rule {rule_id} not found in {p.name}")
    lines = text.splitlines(keepends=True)
    old_tier = ""
    edited = False
    for i in range(span[0], span[1]):
        m = re.match(r"^(\s*)tier:\s*(\S+)\s*$", lines[i])
        if m:
            old_tier = m.group(2)
            lines[i] = f"{m.group(1)}tier: {tier}\n"
            edited = True
            break
    if not edited:   # rule predates the metadata — insert right after the id line
        indent = re.match(r"^(\s*)-\s", lines[span[0]]).group(1) + "  "
        lines.insert(span[0] + 1, f"{indent}tier: {tier}\n")
    new_text = "".join(lines)
    with _LOCK:
        config.atomic_write_text(p, new_text)
    return {"rule_id": rule_id, "old_tier": old_tier, "new_tier": tier,
            "hash_unchanged": True}


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
