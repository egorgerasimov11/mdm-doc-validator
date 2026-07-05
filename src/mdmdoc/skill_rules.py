#!/usr/bin/env python3
"""skill_rules.py — read an mdm-*-checker skill's rule store into a normalized
list, and show how each rule maps to the mdmdoc validator.

The skills (mdm-w9-checker, mdm-banking-checker …) are the human source of truth
for SAP MDM checking. Their `references/dynamic_rules.md` holds structured DR
entries (id / Status / Severity / Action / Scope / Rule / Reason). This module
parses them deterministically so the `mdmdoc-skill-sync` procedure is grounded:
mechanizable rules become explicit validator rules (rules/*.yaml); the rest are
surfaced as advisory (they need SAP-request context the document alone lacks).

READ-ONLY: this module never writes — rule edits go through rules_io.py.
"""
from __future__ import annotations

import re
from pathlib import Path

# Default skill locations (first that exists wins).
_SKILL_ROOTS = [Path.home() / ".claude" / "skills",
                Path.home() / ".openclaw" / "skills"]

# Curated map: which validator rule id / mechanism already covers a DR rule, or
# why it can't be a document-only check. Maintained as rules are promoted.
COVERAGE: dict[str, str] = {
    "DR-20260608-121112": "W9-010 (ein_shape: EIN/SSN must be 9 digits)",
    "DR-20260624-131532": "W9-011 (tin_type_vs_classification) + W9-012 (individual+business+EIN)",
    "DR-20260528-195622": "W9-013 (line_swap_suspect: Line1/Line2 order)",
    "DR-20260608-122211": "extraction: address taken from the W-9 only (no invented components)",
    "DR-20260608-121850": "advisory: TIN hyphen formatting — not a document verdict rule",
    "DR-20260705-170537": "advisory: architecture — surface DO payment-field notes as warnings, "
                          "route to the form owner (needs packet/DO context, not a W-9 field check)",
    "DR-20260629-185548": "needs SAP context: tax category vs address country (not in the document)",
    "DR-20260530-145106": "needs SAP context: US CoCd + Tax Number presence",
    "DR-20260519-164834": "needs SAP context: company-code extension scope",
    "DR-20260518-233827": "needs SAP context: changed W-9/W-8 indicator on an update",
    "DR-20260429-191139": "advisory: withholding code is AP-determined post-submission",
    "DR-20260429-185837": "advisory: withholding 07 is an AP instruction, not a default",
}


def resolve_skill(name_or_path: str) -> Path:
    """Accept a skill name (mdm-w9-checker), a skill dir, or a dynamic_rules.md path."""
    p = Path(name_or_path).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        cand = p / "references" / "dynamic_rules.md"
        return cand if cand.exists() else p
    for root in _SKILL_ROOTS:
        cand = root / name_or_path / "references" / "dynamic_rules.md"
        if cand.exists():
            return cand
    raise FileNotFoundError(f"skill rules not found for {name_or_path!r}")


_HEADER = re.compile(r"^###\s+(DR-\d{8}-\d{6})\s+—\s+(.*)$")
_FIELD = re.compile(r"^-\s+([A-Za-z ]+):\s*(.*)$")


def parse_dynamic_rules(path: str | Path) -> list[dict]:
    """Parse dynamic_rules.md into a list of dicts (id, header, status, severity,
    action, scope, origin, rule, reason). Deterministic; ignores prose around."""
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^(?=###\s+DR-)", text)
    out: list[dict] = []
    for block in blocks:
        m = _HEADER.match(block.splitlines()[0]) if block.strip().startswith("### DR-") else None
        if not m:
            continue
        entry = {"id": m.group(1), "header": m.group(2).strip(),
                 "status": "", "severity": "", "action": "", "scope": "",
                 "origin": "", "rule": "", "reason": ""}
        for line in block.splitlines()[1:]:
            fm = _FIELD.match(line)
            if fm:
                key = fm.group(1).strip().lower()
                if key in entry:
                    entry[key] = fm.group(2).strip()
        entry["rule"] = _section(block, "Rule:")
        entry["reason"] = _section(block, "Reason:")
        entry["coverage"] = COVERAGE.get(entry["id"], "")
        out.append(entry)
    return out


def _section(block: str, label: str) -> str:
    """Text after a 'Label:' line until the next blank-line-separated section."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == label:
            collected: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.strip() in ("Reason:", "Rule:") or nxt.strip() == "---":
                    break
                collected.append(nxt)
            return "\n".join(collected).strip()
    return ""


def active_rules(path: str | Path) -> list[dict]:
    return [r for r in parse_dynamic_rules(path) if r["status"].upper() == "ACTIVE"]
