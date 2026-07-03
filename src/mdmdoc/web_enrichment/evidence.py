#!/usr/bin/env python3
"""
evidence.py — the value object every web_enrichment connector emits.

An Evidence is a HINT from an external source, never a decision. The single
choke point `to_finding()` produces a Finding that is ALWAYS severity=NOTE and
verdict_effect=None — it is structurally impossible for a web source to move the
verdict. The rule engine + YAML rules remain the only deciders (invariant #1).

Source trust tiers (ROADMAP):
  1  official registry / regulator (IRS, FDIC, Federal Reserve, SEC, GLEIF, OFAC, SAM)
  2  official institution domain (a bank's own site)
  3  aggregators / catalogues (not decision-grade)
  4  everything else (not decision-grade — we never emit tier 4)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Evidence status vocabulary (kept small and explicit so the UI can style it).
FOUND = "found"              # a source confirms the document's claim
CONFLICT = "conflict"        # a source CONTRADICTS the document (still only a hint)
NOT_FOUND = "not_found"      # checked, nothing matched
UNAVAILABLE = "unavailable"  # network off / source unreachable / connector not configured
STATUSES = (FOUND, CONFLICT, NOT_FOUND, UNAVAILABLE)

TIER_NAMES = {1: "Tier 1 · official registry", 2: "Tier 2 · official domain",
              3: "Tier 3 · aggregator"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Evidence:
    check: str                 # stable key: "aba_checksum", "fdic_bank", "swift_syntax", ...
    status: str                # one of STATUSES
    label: str                 # one-line human summary (already safe to display)
    source_name: str           # "FDIC BankFind", "GLEIF", "SEC EDGAR", ...
    source_tier: int           # 1 | 2 | 3
    rule_id: str               # "WEB-FDIC-1" etc. — shows in the findings timeline
    source_url: str = ""       # clickable provenance link (empty if none)
    detail: str = ""           # extra context (already masked/safe)
    query: str = ""            # exactly what left the machine (allowed identifiers only)
    ts: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            self.status = UNAVAILABLE
        if self.source_tier not in (1, 2, 3):
            self.source_tier = 3

    def to_row(self) -> dict:
        """Row for the 'External evidence' UI panel and the web_evidence.json artifact."""
        return {"check": self.check, "status": self.status, "label": self.label,
                "source": self.source_name, "tier": self.source_tier,
                "tier_name": TIER_NAMES.get(self.source_tier, "Tier 3"),
                "url": self.source_url, "detail": self.detail,
                "query": self.query, "ts": self.ts, "rule_id": self.rule_id}

    def to_finding(self):
        """THE choke point: every web finding is a NOTE with NO verdict effect.
        This is the code-level guarantee that external sources cannot decide."""
        from ..rules.engine import Finding
        tail = f" [{self.source_name}, {self.ts}]"
        msg = f"External evidence ({self.status}): {self.label}.{tail} " \
              "Advisory only — web did not decide this verdict."
        return Finding(self.rule_id, "NOTE", None, msg, self.detail)
