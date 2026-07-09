#!/usr/bin/env python3
"""
rule_propose.py — turn an operator's free-text feedback on a run into a PROPOSED
edit of the explicit YAML rules. The model only PROPOSES; a human reviews and
applies (invariant #1: verdicts come from explicit rules, never the model).

Flow: operator types "BNK-011 shouldn't fire on US docs — the US has no IBAN" on
the run page -> `propose()` loads the run's fired findings + the rule blocks that
fired + the extracted fields, asks the strong model to classify and (if it is a
rule problem) emit a corrected rule block -> we splice that block into the YAML
*textually* (deterministic, preserves the rest of the file/comments) and validate
it -> the run page shows the diff for the operator to apply via the existing
`rules_io.save_rules` choke point.

Classification (kind), mirroring `scenarios.ERROR_SOURCES`:
  * "rule"       — fields are right, the VERDICT/rule is wrong -> edit rules/*.yaml.
  * "extraction" — a field was read wrong -> route to the existing Correct/teach flow.
  * "needs_code" — the fix needs a NEW predicate that does not exist yet -> escalate
                   (Python predicate + hand-port to ABAP; not applied here).

This module never writes: it returns proposals. Applying goes through
`rules_io.save_rules` (the sole rule writer). It also never persists the feedback.
"""
from __future__ import annotations

import difflib
import re

import yaml

from . import rules_io, runstore
from .rules.engine import SEVERITIES, VERDICTS
from .rules.predicates import REGISTRY as PREDICATES

# the `when:` operator vocabulary the engine understands (engine._eval_when)
WHEN_OPS = {"always", "field_missing", "flag_true", "flag_false",
            "equals", "in", "regex_mismatch", "check"}
KINDS = ("rule", "extraction", "needs_code")
_DIGIT_RUN = re.compile(r"\d{7,}")


# --------------------------------------------------------------- run context --
def _load_ctx(run_id: str) -> dict:
    rid = runstore.resolve_run(run_id)
    if not rid:
        raise RunNotFound(run_id)
    meta = runstore.load(rid, "meta.json") or {}
    doc_class = meta.get("doc_class", "bank")
    if doc_class not in ("bank", "w9"):
        doc_class = "bank"
    findings = runstore.load(rid, "findings.json") or []
    pub = runstore.load(rid, "extraction.json") or {}
    rep = runstore.load(rid, "report.json")
    if isinstance(rep, str):
        import json
        rep = json.loads(rep)
    return {"rid": rid, "doc_class": doc_class, "meta": meta,
            "findings": findings, "pub": pub, "report": rep or {}}


class RunNotFound(RuntimeError):
    pass


# --------------------------------------------------------- textual YAML edit --
def _rule_block_span(text: str, rule_id: str) -> tuple[int, int] | None:
    """(start, end) line indices of the ``  - id: <rule_id>`` block, end
    exclusive. A block runs until the next list item or a column-0 line."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*-\s*id:\s*(\S+)", ln)
        if m and m.group(1).strip().strip('"\'') == rule_id:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if re.match(r"^\s*-\s", ln) or (ln and not ln[0].isspace()):
            return start, j
    return start, len(lines)


def _block_text(rule: dict) -> str:
    """Serialize ONE rule dict to the file's style: list item at 2 spaces, keys
    at 4. default_flow_style=None keeps scalar-only maps/lists inline
    (`when: {check: …}`, `applies_to: [bank_letter]`) so the diff of a small
    change stays small instead of reflowing the whole block."""
    dumped = yaml.safe_dump([rule], sort_keys=False, default_flow_style=None,
                            allow_unicode=True, width=4096)
    return "\n".join(("  " + ln).rstrip() for ln in dumped.splitlines())


def _carry_metadata(text: str, rid: str, rule: dict) -> dict:
    """Preserve governance metadata (tier/source) from the existing rule when an
    edit proposal omits it (the model never sees those keys)."""
    try:
        cfg = yaml.safe_load(text) or {}
        old = next((r for r in cfg.get("rules", [])
                    if isinstance(r, dict) and str(r.get("id")) == str(rid)), None)
    except Exception:
        old = None
    if not old:
        return rule
    merged = dict(rule)
    for k in ("tier", "source"):
        if k not in merged and k in old:
            merged[k] = old[k]
    return merged


def apply_change(text: str, action: str, rule: dict, rule_id: str = "") -> str:
    """Deterministically splice a proposed change into the rule file TEXT.
    Preserves the rest of the file (header, comments, other rules) verbatim."""
    rid = rule_id or (rule or {}).get("id", "")
    lines = text.splitlines(keepends=True)
    if action == "remove":
        span = _rule_block_span(text, rid)
        if not span:
            raise ValueError(f"rule {rid} not found to remove")
        del lines[span[0]:span[1]]
        return "".join(lines)
    if action == "edit":
        span = _rule_block_span(text, rid)
        if not span:
            raise ValueError(f"rule {rid} not found to edit")
        # governance metadata (tier/source) is invisible to the model's proposal
        # — carry it over from the existing rule so an edit never silently drops
        # it (the approval survives regardless via the denylist hash, but the
        # annotation must not be lost).
        rule = _carry_metadata(text, rid, rule)
        block = _block_text(rule).rstrip("\n") + "\n"
        lines[span[0]:span[1]] = [block]
        return "".join(lines)
    block = _block_text(rule).rstrip("\n") + "\n"
    if action == "add":
        # append after the last rule block (before any trailing top-level key)
        last = None
        for i, ln in enumerate(text.splitlines()):
            if re.match(r"^\s*-\s*id:\s*\S+", ln):
                sp = _rule_block_span(text, re.match(r"^\s*-\s*id:\s*(\S+)", ln).group(1).strip('"\''))
                if sp:
                    last = sp[1]
        src = text.splitlines(keepends=True)
        at = last if last is not None else len(src)
        src[at:at] = ["\n" + block]
        return "".join(src)
    raise ValueError(f"unknown action {action!r}")


# ------------------------------------------------------------- validation -----
def _doc_types(doc_class: str) -> set[str]:
    try:
        cfg = yaml.safe_load(rules_io.rules_path(doc_class).read_text()) or {}
        return set(cfg.get("doc_types", []) or [])
    except Exception:
        return set()


def validate_rule(rule: dict, doc_class: str) -> list[str]:
    """Semantic checks `save_rules` does NOT do (it only checks parse + dup id).
    A rule that references an unknown predicate would silently become an
    engine_error NOTE at runtime — catch it here before offering 'apply'."""
    issues: list[str] = []
    if not isinstance(rule, dict):
        return ["proposed rule is not a mapping"]
    if not str(rule.get("id", "")).strip():
        issues.append("rule has no id")
    sev = rule.get("severity")
    if sev not in SEVERITIES:
        issues.append(f"severity {sev!r} not in {SEVERITIES}")
    ve = rule.get("verdict_effect")
    if ve is not None and ve not in VERDICTS:
        issues.append(f"verdict_effect {ve!r} not in {VERDICTS} or null")
    when = rule.get("when")
    if not isinstance(when, dict) or not when:
        issues.append("`when` must be a non-empty mapping")
    else:
        op = next(iter(when))
        if op not in WHEN_OPS:
            issues.append(f"`when` operator {op!r} not in {sorted(WHEN_OPS)}")
        if "check" in when and when["check"] not in PREDICATES:
            issues.append(f"check predicate {when['check']!r} does not exist "
                          f"(known: {sorted(PREDICATES)}) — needs_code")
    at = rule.get("applies_to")
    if at is not None:
        known = _doc_types(doc_class)
        bad = [t for t in at if known and t not in known]
        if bad:
            issues.append(f"applies_to has unknown doc_type(s): {bad}")
    # privacy: a rule message must never carry a document value (account/IBAN)
    for k in ("message", "message_ru"):
        if _DIGIT_RUN.search(str(rule.get(k, ""))):
            issues.append(f"{k} contains a long digit run — a rule message must not "
                          "embed document values; use {value_masked}/{detail} placeholders")
    return issues


# ------------------------------------------------------------- the proposer ---
def _prompt(feedback: str, ctx: dict, cur_yaml: str, rule_id: str = "") -> str:
    fired = []
    cfg = yaml.safe_load(cur_yaml) or {}
    by_id = {r.get("id"): r for r in cfg.get("rules", []) if isinstance(r, dict)}
    for f in ctx["findings"]:
        rid = f.get("rule_id") if isinstance(f, dict) else None
        blk = by_id.get(rid)
        fired.append({"rule_id": rid, "severity": f.get("severity"),
                      "message": f.get("message"),
                      "rule_block": blk})
    if rule_id:   # the operator clicked "dispute" on one finding — put it first
        fired.sort(key=lambda x: x.get("rule_id") != rule_id)
    fields = {k: (v.get("masked") if isinstance(v, dict) else v)
              for k, v in (ctx["pub"].get("fields") or {}).items()}
    target = (f"THE OPERATOR IS SPECIFICALLY DISPUTING RULE {rule_id} — focus on it "
              "(it may still turn out to be an extraction/needs_code issue).\n\n"
              if rule_id else "")
    return (
        target +
        "OPERATOR FEEDBACK (free text — what they think is wrong):\n"
        f"{feedback}\n\n"
        f"DOCUMENT TYPE: {ctx['pub'].get('doc_type')}   VERDICT: {ctx['report'].get('verdict')}\n"
        f"EXTRACTED FIELDS (masked): {fields}\n\n"
        "RULES THAT FIRED on this document (id, severity, message, and the exact "
        "YAML block of each):\n"
        f"{yaml.safe_dump(fired, sort_keys=False, allow_unicode=True)}\n"
        f"VALID check predicates: {sorted(PREDICATES)}\n"
        f"VALID severities: {list(SEVERITIES)}   verdict_effect: {list(VERDICTS)} or null\n"
        f"VALID when operators: {sorted(WHEN_OPS)}\n\n"
        "Classify and respond with the JSON schema from your instructions."
    )


def propose(run_id: str, feedback: str, rule_id: str = "") -> dict:
    """Classify the feedback and, if it is a rule problem, return a proposed
    rule-file edit + diff for the operator to review. `rule_id` (optional) is the
    specific fired rule the operator clicked 'dispute' on. Never writes."""
    from . import config, model_client as mc
    ctx = _load_ctx(run_id)
    cur_yaml = rules_io.rules_path(ctx["doc_class"]).read_text()
    system = (config.PROMPTS_DIR / "system_rule_propose.txt").read_text()
    obj, _ok = mc.generate_json("TEXT_STRONG", _prompt(feedback, ctx, cur_yaml, rule_id),
                                system=system,
                                options={"temperature": 0, "seed": 7, "num_predict": 2048})
    mc.unload("TEXT_STRONG")
    if not isinstance(obj, dict):
        return {"kind": "error", "rationale": "the model did not return a usable proposal — "
                "rephrase your feedback and try again."}
    kind = obj.get("kind") if obj.get("kind") in KINDS else "rule"
    out = {"kind": kind, "doc_class": ctx["doc_class"], "run_id": ctx["rid"],
           "rationale": str(obj.get("rationale") or obj.get("reason") or "")}

    if kind == "extraction":
        out["route"] = f"/ui/runs/{ctx['rid']}/review"
        out["hint"] = ("a field was read wrong, not a rule error — correct it in the "
                       "teach flow (Correct — teach the model).")
        return out
    if kind == "needs_code":
        out["needs_code"] = str(obj.get("needs_code") or "a new predicate is required")
        out["hint"] = ("this fix needs new predicate logic (not in the rule engine yet) — "
                       "it must be coded in predicates.py and hand-ported to ABAP "
                       "(see PARITY.md / tools/check_parity.py).")
        return out

    # kind == "rule": build + validate the proposed edit
    action = obj.get("action") if obj.get("action") in ("edit", "add", "remove") else "edit"
    rule = obj.get("rule") or {}
    rule_id = str(obj.get("rule_id") or rule.get("id") or "")
    out.update({"action": action, "rule_id": rule_id})
    issues = validate_rule(rule, ctx["doc_class"]) if action != "remove" else []
    try:
        proposed = apply_change(cur_yaml, action, rule, rule_id)
        # structural gate: parseable, rules: list, no dup ids (mirror save_rules)
        parsed = yaml.safe_load(proposed)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list):
            issues.append("result is not a valid rules file")
        else:
            ids = [r.get("id") for r in parsed["rules"] if isinstance(r, dict)]
            if len(ids) != len(set(ids)):
                issues.append("duplicate rule id after the change")
    except Exception as e:  # noqa: BLE001 — surface, never crash the run
        issues.append(f"could not apply the change: {e}")
        proposed = cur_yaml
    span = _rule_block_span(cur_yaml, rule_id)
    cur_block = "\n".join(cur_yaml.splitlines()[span[0]:span[1]]) if span else ""
    out.update({
        "current_yaml": cur_yaml, "proposed_yaml": proposed,
        "current_block": cur_block,
        "diff": "".join(difflib.unified_diff(
            cur_yaml.splitlines(keepends=True), proposed.splitlines(keepends=True),
            fromfile=f"{ctx['doc_class']}.yaml (current)",
            tofile=f"{ctx['doc_class']}.yaml (proposed)")),
        "validation": issues,
        "applicable": not issues,
    })
    return out
