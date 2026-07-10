#!/usr/bin/env python3
"""
rule_wizard.py — guided rule creation (F2b): the operator writes ONE free-text
sentence at the top of the Approvals panel, the local strong model drafts an
explicit YAML rule, and a LIVE questionnaire — one question, exactly three
answer buttons — settles what the draft left ambiguous (which document types,
which country, how strict). The finished rule is appended PENDING: the wizard
authors, the approval panel decides — never the other way around.

The questionnaire is DETERMINISTIC (built from the draft's gaps), so a small
local model only has to draft; robustness does not depend on it phrasing
questions. It may add at most ONE clarify question of its own, and only a
well-shaped one survives.
"""
from __future__ import annotations

import re

from . import rules_io
from .rule_propose import validate_rule

# marker the wizard uses for the "no scope restriction" answers
ALL_TYPES = "all document types"
ALL_COUNTRIES = "all countries"

_STRICTNESS = (
    ("REJECT — refuse the document", "CRITICAL", "REJECT"),
    ("NEED_MANUAL_REVIEW — hold for a human", "WARNING", "NEED_MANUAL_REVIEW"),
    ("WARNING — flag but allow", "WARNING", "WARNING"),
)


def _doc_types(doc_class: str) -> list[str]:
    import yaml
    cfg = yaml.safe_load(rules_io.rules_text(doc_class) or "") or {}
    return [str(t) for t in cfg.get("doc_types") or []]


def _existing_ids(doc_class: str) -> list[str]:
    import yaml
    cfg = yaml.safe_load(rules_io.rules_text(doc_class) or "") or {}
    return [str(r.get("id") or "") for r in cfg.get("rules") or []
            if isinstance(r, dict)]


def next_rule_id(doc_class: str) -> str:
    """First free id above the highest numeric one — BNK-048 after BNK-047.
    The save path still rejects duplicates, this just avoids proposing one."""
    prefix = "BNK" if doc_class == "bank" else "W9"
    top = 0
    for rid in _existing_ids(doc_class):
        m = re.fullmatch(rf"{prefix}-(\d+)", rid)
        if m:
            top = max(top, int(m.group(1)))
    return f"{prefix}-{top + 1:03d}"


def draft(text: str, doc_class: str) -> dict:
    """Free text -> {rule draft, rationale, clarify?} via the strong model.
    Model call only — everything after (questions, merge, validation, save)
    is deterministic."""
    from . import config, model_client as mc
    from .rule_propose import PREDICATES, SEVERITIES, VERDICTS, WHEN_OPS
    system = (config.PROMPTS_DIR / "system_rule_create.txt").read_text()
    prompt = (
        f"Document class: {doc_class}\n"
        f"VALID document types: {_doc_types(doc_class)}\n"
        f"VALID check predicates: {sorted(PREDICATES)}\n"
        f"VALID severities: {list(SEVERITIES)}   verdict_effect: {list(VERDICTS)} or null\n"
        f"VALID when operators: {sorted(WHEN_OPS)}\n\n"
        f"The operator's rule, verbatim:\n{text.strip()}\n\n"
        "Draft the rule and respond with the JSON schema from your instructions."
    )
    obj, _ok = mc.generate_json("TEXT_STRONG", prompt, system=system,
                                options={"temperature": 0, "seed": 7,
                                         "num_predict": 1536})
    mc.unload("TEXT_STRONG")
    if not isinstance(obj, dict) or not isinstance(obj.get("rule"), dict):
        return {"ok": False,
                "rationale": "the model did not return a usable draft — "
                             "rephrase the rule and try again."}
    rule = dict(obj["rule"])
    rule.pop("id", None)                     # ids are assigned, never drafted
    rule.setdefault("tier", "experimental")
    rule.setdefault("source", "operator")
    return {"ok": True, "rule": rule,
            "rationale": str(obj.get("rationale") or ""),
            "clarify": obj.get("clarify"),
            "questions": questions({"rule": rule, "clarify": obj.get("clarify")},
                                   doc_class)}


def questions(draft_obj: dict, doc_class: str) -> list[dict]:
    """The gaps of a draft, as a LIVE questionnaire: one question + exactly
    three answer buttons each (Egor's shape). Deterministic — the model's own
    clarify question is appended only when well-shaped, and only one."""
    rule = draft_obj.get("rule") or {}
    out: list[dict] = []
    types = _doc_types(doc_class)
    if not rule.get("applies_to"):
        first = types[0] if types else "bank_letter"
        second = types[1] if len(types) > 1 else ALL_TYPES
        out.append({"key": "applies_to",
                    "question": "Which documents does this rule apply to?",
                    "options": [first, second, ALL_TYPES]})
    if "verdict_effect" not in rule or rule.get("verdict_effect") == "":
        out.append({"key": "strictness",
                    "question": "How strictly should it act?",
                    "options": [o[0] for o in _STRICTNESS]})
    cc = [str(c).upper() for c in rule.get("countries") or []]
    if cc:
        out.append({"key": "countries",
                    "question": f"Apply only to {', '.join(cc)} documents?",
                    "options": [f"only {', '.join(cc)}", ALL_COUNTRIES,
                                "other country"]})
    clarify = draft_obj.get("clarify")
    if (isinstance(clarify, dict) and str(clarify.get("question") or "").strip()
            and isinstance(clarify.get("options"), list)
            and len(clarify["options"]) == 3
            and all(str(o).strip() for o in clarify["options"])):
        out.append({"key": "clarify",
                    "question": str(clarify["question"])[:160],
                    "options": [str(o)[:80] for o in clarify["options"]]})
    return out[:4]


def apply_answers(rule: dict, answers: dict, doc_class: str) -> dict:
    """Merge questionnaire answers into the draft -> a rule ready for
    validate_rule + the add-splice. Unknown answers are ignored (the buttons
    are the contract); the free-text 'other country' answer carries ISO2."""
    r = dict(rule)
    a_types = str(answers.get("applies_to") or "").strip()
    if a_types:
        r["applies_to"] = None if a_types == ALL_TYPES else [a_types]
        if r["applies_to"] is None:
            r.pop("applies_to")
    strict = str(answers.get("strictness") or "").strip()
    for label, sev, eff in _STRICTNESS:
        if strict == label or strict.split(" ")[0] == eff:
            r["severity"], r["verdict_effect"] = sev, eff
    cc_ans = str(answers.get("countries") or "").strip()
    if cc_ans:
        if cc_ans == ALL_COUNTRIES:
            r.pop("countries", None)
        elif cc_ans.startswith("only "):
            pass                              # keep the drafted list
        else:
            iso = re.findall(r"\b([A-Z]{2})\b", cc_ans.upper())
            if iso:
                r["countries"] = iso
    # the model's own clarify question has no structured slot — the operator's
    # button choice travels in the oplog detail, nothing merges structurally
    r.setdefault("severity", "WARNING")
    if "verdict_effect" not in r:
        r["verdict_effect"] = None
    r.setdefault("message", r.get("name", "rule"))
    if not str(r.get("message_ru") or "").strip():
        r["message_ru"] = r["message"]
    r.setdefault("tier", "experimental")
    r.setdefault("source", "operator")
    # id FIRST: every rule block in the file starts `- id:` — the block-span
    # scanner and the delete backups rely on that shape
    rid = r.pop("id", "") or next_rule_id(doc_class)
    return {"id": rid, **r}


def create(rule: dict, doc_class: str) -> dict:
    """Validate + append the finished rule as a PENDING block. Returns
    {ok, rule_id, issues} — issues non-empty means nothing was written."""
    from .rule_propose import apply_change
    issues = validate_rule(rule, doc_class)
    if issues:
        return {"ok": False, "issues": issues, "rule_id": rule.get("id")}
    text = rules_io.rules_text(doc_class)
    new_text = apply_change(text, "add", rule, str(rule["id"]))
    rules_io.save_rules(doc_class, new_text)
    return {"ok": True, "rule_id": str(rule["id"]), "issues": []}
