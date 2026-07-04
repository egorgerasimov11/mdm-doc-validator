#!/usr/bin/env python3
"""verdict.py — fold rule findings into a single verdict (rule engine decides,
never the model). Precedence: REJECT > NEED_MANUAL_REVIEW > WARNING > ACCEPT."""
from __future__ import annotations

from .rules.engine import Finding

_PRECEDENCE = ["REJECT", "NEED_MANUAL_REVIEW", "WARNING", "ACCEPT"]

_NEXT_STEP = {
    "bank": {
        "ACCEPT": "use as banking support — compare against SAP/the request form before entry",
        "WARNING": "usable with caution — note the warnings for the Data Owner",
        "NEED_MANUAL_REVIEW": "hold — resolve the flagged items manually before use",
        "REJECT": "kick back — request a bank letter / bank statement / supplier letterhead",
    },
    "w9": {
        "ACCEPT": "use for SAP entry (Line 1 -> Name 1, Line 2 -> Name 2, TIN per type)",
        "WARNING": "usable with caution — note the warnings",
        "NEED_MANUAL_REVIEW": "hold — clarify with requestor or request an updated form",
        "REJECT": "kick back — request a corrected W-9",
    },
}


def decide(findings: list[Finding], extra_findings: list[Finding] | None = None) -> str:
    """extra_findings is the v2 hook for the vendor-template compare mode."""
    all_f = list(findings) + list(extra_findings or [])
    effects = {f.verdict_effect for f in all_f if f.verdict_effect}
    for v in _PRECEDENCE[:-1]:
        if v in effects:
            return v
    return "ACCEPT"


def next_step(doc_class: str, verdict: str) -> str:
    return _NEXT_STEP.get(doc_class, _NEXT_STEP["bank"]).get(verdict, "")


def exit_code(verdict: str) -> int:
    from . import config
    return {"ACCEPT": config.EXIT_ACCEPT, "REJECT": config.EXIT_REJECT}.get(verdict, config.EXIT_REVIEW)
