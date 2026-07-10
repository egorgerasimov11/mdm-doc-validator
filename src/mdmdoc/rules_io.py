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


UNIFIED_NAME = "rules.yaml"
_SECTION_RE = re.compile(r"(?m)^--- # doc_class: (\w+)\s*$")
_HEADER = (
    "# mdmdoc unified rules — the ONE physical rules file (D9, operator decision).\n"
    "# Multi-document YAML: each '--- # doc_class: X' line starts that class's\n"
    "# section, whose text is byte-identical to the former per-class file — rule\n"
    "# block hashes (approvals) and in-block comments survive untouched.\n"
    "# Learned/skill rules are appended into the same sections with source: tags.\n")


def unified_path() -> Path:
    return config.RULES_DIR / UNIFIED_NAME


def rules_path(doc_class: str) -> Path:
    """LEGACY per-class path (pre-unified layout) — kept for fallback reads."""
    return config.RULES_DIR / ("banking.yaml" if doc_class == "bank" else "w9.yaml")


def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    marks = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections[m.group(1)] = text[m.end():end].lstrip("\n")
    return sections


def _assemble(sections: dict[str, str]) -> str:
    parts = [_HEADER]
    for name in sorted(sections):
        body = sections[name].rstrip("\n") + "\n"
        parts.append(f"--- # doc_class: {name}\n{body}")
    return "\n".join(parts)


def unified_text() -> str:
    p = unified_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def rules_text(doc_class: str) -> str:
    """THE canonical rule-text read: the class section of the unified file,
    with a legacy per-class-file fallback for transitional checkouts."""
    u = unified_path()
    if u.exists():
        return _split_sections(u.read_text(encoding="utf-8")).get(doc_class, "")
    legacy = rules_path(doc_class)
    return legacy.read_text(encoding="utf-8") if legacy.exists() else ""


def _write_rules_text(doc_class: str, text: str) -> None:
    """Write one class's section back (unified when present, else legacy)."""
    u = unified_path()
    if u.exists():
        sections = _split_sections(u.read_text(encoding="utf-8"))
        sections[doc_class] = text
        config.atomic_write_text(u, _assemble(sections))
    else:
        config.atomic_write_text(rules_path(doc_class), text)


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
        _write_rules_text(doc_class, text)
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
    text = rules_text(doc_class)
    span = _rule_block_span(text, rule_id)
    if not span:
        raise ValueError(f"rule {rule_id} not found in the {doc_class} rules")
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
        _write_rules_text(doc_class, new_text)
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
        if unified_path().exists():
            shutil.copy(unified_path(), abap / "rules" / UNIFIED_NAME)
        else:   # legacy layout
            for f in ("banking.yaml", "w9.yaml"):
                shutil.copy(config.RULES_DIR / f, abap / "rules" / f)
        r = subprocess.run(["python3", str(gen)], cwd=str(abap),
                           capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        return {"ok": False, "detail": str(e)}
    return {"ok": r.returncode == 0, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}


def save_unified(text: str) -> dict:
    """Whole-file save from the rules editor: every section must validate.
    Returns per-class rule counts."""
    sections = _split_sections(text)
    if not sections:
        raise ValueError("no '--- # doc_class: <name>' sections found")
    counts: dict[str, int] = {}
    parsed_ok: dict[str, str] = {}
    for doc_class, body in sections.items():
        try:
            parsed = yaml.safe_load(body)
        except yaml.YAMLError as e:
            raise ValueError(f"[{doc_class}] YAML parse error: {e}")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list):
            raise ValueError(f"[{doc_class}] top level must be a mapping with a 'rules:' list")
        ids = [r.get("id") for r in parsed["rules"] if isinstance(r, dict)]
        if len(ids) != len(set(ids)):
            raise ValueError(f"[{doc_class}] duplicate rule id")
        from .rule_propose import validate_rule
        known = set(parsed.get("doc_types") or [])
        for r in parsed["rules"]:
            if not isinstance(r, dict):
                raise ValueError(f"[{doc_class}] a rules: entry is not a mapping")
            issues = validate_rule(r, doc_class, known_doc_types=known)
            if issues:
                raise ValueError(f"[{doc_class}] rule {r.get('id', '?')}: "
                                 + "; ".join(issues))
        counts[doc_class] = len(parsed["rules"])
        parsed_ok[doc_class] = body
    with _LOCK:
        config.atomic_write_text(unified_path(), _assemble(parsed_ok))
    return counts
