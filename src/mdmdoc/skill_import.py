#!/usr/bin/env python3
"""
skill_import.py — attach a SKILL as a rule source from the dashboard (D10).

The operator uploads a skill (SKILL.md / any .md / a .zip of the skill folder)
and the system extracts rule candidates and appends them to the rules file as
PENDING (source: skill:<name>, tier: experimental). The approval hash gate is
untouched: nothing fires until Egor approves each rule in the panel — the
'human approves rules' invariant survives by construction.

Two extraction paths:
  * mdm-*-checker style (a references/dynamic_rules.md with DR entries):
    parsed DETERMINISTICALLY via skill_rules.parse_dynamic_rules — no model;
  * arbitrary skill text: the strong model proposes rule blocks, each block
    passes rule_propose.validate_rule (known predicates/operators only).

Re-importing a skill of the same name REPLACES its previous rules (any rule
whose source is skill:<name>) — new/changed blocks naturally land as PENDING.
Only ADDITIVE-or-own-replacement edits are possible: rules from other sources
are never touched.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import yaml

from . import config, rules_io, skill_rules
from .rule_propose import validate_rule


def skills_dir() -> Path:
    return config.RULES_DIR / "skills"


def _safe_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9\-_]+", "-", (name or "skill").lower()).strip("-")
    return n[:60] or "skill"


def store_upload(name: str, filename: str, data: bytes) -> Path:
    """Persist the uploaded skill for provenance/re-import under
    rules/skills/<name>/ ; zips are expanded, single files stored as-is."""
    root = skills_dir() / _safe_name(name)
    root.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename or "SKILL.md").suffix.lower()
    if suffix == ".zip":
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(root)   # extractall sanitizes member paths
    else:
        (root / (Path(filename).name or "SKILL.md")).write_text(
            data.decode("utf-8", errors="replace"), encoding="utf-8")
    return root


def list_imported() -> list[dict]:
    """[{name, files, rule_count}] — what the operator has attached, and how
    many live rules carry each skill's source tag."""
    out = []
    if not skills_dir().exists():
        return out
    counts: dict[str, int] = {}
    for dc in ("bank", "w9"):
        try:
            cfg = yaml.safe_load(rules_io.rules_text(dc)) or {}
        except Exception:
            continue
        for r in cfg.get("rules", []) or []:
            src = str((r or {}).get("source", ""))
            if src.startswith("skill:"):
                counts[src[6:]] = counts.get(src[6:], 0) + 1
    for d in sorted(skills_dir().iterdir()):
        if d.is_dir():
            out.append({"name": d.name,
                        "files": sorted(p.name for p in d.rglob("*") if p.is_file())[:8],
                        "rule_count": counts.get(d.name, 0)})
    return out


# --- extraction -----------------------------------------------------------------
_SEV_MAP = {"critical": "CRITICAL", "high": "CRITICAL", "warning": "WARNING",
            "medium": "WARNING", "note": "NOTE", "low": "NOTE", "info": "NOTE"}

_EXTRACT_PROMPT = """You convert a HUMAN CHECKING SKILL into explicit validator rules.

Below is the skill text, then the CURRENT rules file of the target class (so you
reuse its doc_types, predicates and style). Extract every rule from the skill
that can be decided from THE DOCUMENT ALONE (no SAP request context) and that
the existing rule set does not already cover.

Return STRICT JSON: {"rules": [ {rule}, ... ], "skipped": ["reason", ...]}
where each {rule} is a mapping with keys exactly like the existing rules:
id (prefix %(prefix)s-9xx, unique), name (snake_case), applies_to (list of the
class's doc_types), when (ONE of: {"always": true} | {"field_missing": "<field>"} |
{"flag_true": "<field>"} | {"flag_false": "<field>"} | {"equals": {"field": f,
"value": v}} | {"check": "<predicate name FROM THE CURRENT FILE ONLY>", "field": f}),
severity (CRITICAL|WARNING|NOTE), verdict_effect (REJECT|NEED_MANUAL_REVIEW|
WARNING|null), message (English, may use {value}/{detail}).
NEVER invent predicate names. Prefer fewer, correct rules. JSON only.

SKILL TEXT:
%(skill)s

CURRENT RULES FILE (%(doc_class)s):
%(rules)s
"""


def _dr_to_rule(entry: dict, doc_class: str, prefix: str, idx: int,
                doc_types: list) -> dict | None:
    """Deterministic best-effort mapping of a DR entry to an ALWAYS-advisory
    rule (a DR rule that needs request context can still surface as a NOTE)."""
    rule_text = (entry.get("rule") or entry.get("header") or "").strip()
    if not rule_text:
        return None
    sev = _SEV_MAP.get(str(entry.get("severity", "")).lower(), "NOTE")
    return {"id": f"{prefix}-9{idx:02d}",
            "name": re.sub(r"[^a-z0-9_]+", "_",
                           (entry.get("header") or entry["id"]).lower())[:40].strip("_"),
            "applies_to": list(doc_types),
            "when": {"always": True},
            "severity": sev,
            "verdict_effect": None,
            "message": f"[skill advisory {entry['id']}] {rule_text[:220]}"}


def extract_rules(skill_root: Path, doc_class: str, log=print,
                  ignore_source: str = "") -> list[dict]:
    """-> validated rule dicts (possibly empty). Deterministic for checker
    skills; strong-model extraction for arbitrary text. ignore_source: this
    skill's OWN previous rules don't count as 'existing' (they are being
    replaced by the re-import)."""
    cfg = yaml.safe_load(rules_io.rules_text(doc_class)) or {}
    doc_types = list(cfg.get("doc_types") or [])
    prefix = "BNK" if doc_class == "bank" else "W9"
    existing_ids = {str(r.get("id")) for r in cfg.get("rules", []) or []
                    if str((r or {}).get("source", "")) != ignore_source}

    dyn = list(skill_root.rglob("dynamic_rules.md"))
    rules: list[dict] = []
    if dyn:
        log(f"checker-skill detected — parsing {dyn[0].name} deterministically")
        entries = [e for e in skill_rules.parse_dynamic_rules(dyn[0])
                   if str(e.get("status", "")).lower() not in ("retired", "rejected")]
        idx = 1
        for e in entries:
            r = _dr_to_rule(e, doc_class, prefix, idx, doc_types)
            if r and r["id"] not in existing_ids:
                rules.append(r)
                idx += 1
    else:
        texts = []
        for p in sorted(skill_root.rglob("*.md"))[:6]:
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
        blob = "\n\n".join(texts)[:24000]
        if not blob.strip():
            return []
        from . import model_client as mc
        log("arbitrary skill — asking the strong model for rule candidates…")
        obj, _ = mc.generate_json(
            "TEXT_STRONG",
            _EXTRACT_PROMPT % {"skill": blob, "doc_class": doc_class,
                               "rules": rules_io.rules_text(doc_class)[:12000],
                               "prefix": prefix},
            options={"temperature": 0, "seed": 7, "num_predict": 3072})
        mc.unload("TEXT_STRONG")
        if isinstance(obj, dict):
            rules = [r for r in obj.get("rules", []) if isinstance(r, dict)]

    # validate every candidate; stamp governance; drop invalid loudly
    out = []
    for r in rules:
        r.setdefault("tier", "experimental")
        issues = validate_rule(r, doc_class, known_doc_types=set(doc_types))
        if issues:
            log(f"  dropped {r.get('id', '?')}: {'; '.join(issues)}")
            continue
        out.append(r)
    return out


def import_skill(name: str, doc_class: str, log=print) -> dict:
    """Extraction + REPLACE-own-then-append into the rules file. Everything
    lands PENDING; other sources' rules are byte-untouched."""
    root = skills_dir() / _safe_name(name)
    if not root.exists():
        raise ValueError(f"skill '{name}' not stored — upload it first")
    src_tag = f"skill:{_safe_name(name)}"
    candidates = extract_rules(root, doc_class, log=log, ignore_source=src_tag)
    for r in candidates:
        r["source"] = src_tag

    cur_text = rules_io.rules_text(doc_class)
    cfg = yaml.safe_load(cur_text) or {}
    kept, replaced = [], 0
    for r in cfg.get("rules", []) or []:
        if str((r or {}).get("source", "")) == src_tag:
            replaced += 1
        else:
            kept.append(r)
    if not candidates and not replaced:
        log("nothing extractable and nothing to replace — rules file untouched")
        return {"imported": 0, "replaced": 0}

    # rebuild the file TEXTUALLY: this skill's rules only ever live inside its
    # own MARKED segment (we control both ends), so replacement = drop the
    # segment, append a fresh one; every other byte stays identical
    seg_re = re.compile(
        rf"\n  # --- imported from {re.escape(src_tag)} \(PENDING until approved\) ---\n"
        rf"(?:.*?)(?=\n  # --- imported from skill:|\Z)", re.S)
    text = seg_re.sub("", cur_text)
    additions = "".join(
        "\n" + "\n".join("  " + ln for ln in
                         yaml.safe_dump([r], sort_keys=False,
                                        allow_unicode=True,
                                        default_flow_style=False).rstrip("\n")
                         .splitlines())
        + "\n"
        for r in candidates)
    if additions:
        header = (f"\n  # --- imported from {src_tag} "
                  f"(PENDING until approved) ---\n")
        text = text.rstrip("\n") + "\n" + header + additions
    rules_io.save_rules(doc_class, text)
    log(f"{len(candidates)} rule(s) imported as PENDING (source {src_tag}), "
        f"{replaced} previous rule(s) of this skill replaced — approve them "
        "under Rules → Approvals")
    return {"imported": len(candidates), "replaced": replaced,
            "ids": [r["id"] for r in candidates]}
