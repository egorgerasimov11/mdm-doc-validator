#!/usr/bin/env python3
"""bulk.model — row/result contracts shared by every bulk case."""
from __future__ import annotations

from dataclasses import dataclass, field

# Bucket semantics (aligned with the sap-us-bank-validate skill):
#   VALID      — every applicable check passed
#   SUSPICIOUS — checks passed formally but something warrants a human look
#                (duplicate across partners, registry miss, empty-but-expected)
#   INVALID    — a deterministic check FAILED (checksum, format, wrong-country
#                value in the category) — mathematically/structurally wrong
#   SKIPPED    — the value cannot be judged (masked in the export, empty row)
BUCKETS = ("VALID", "SUSPICIOUS", "INVALID", "SKIPPED")

_RANK = {"SKIPPED": 0, "VALID": 1, "SUSPICIOUS": 2, "INVALID": 3}


@dataclass
class RowVerdict:
    row_no: int                       # 1-based DATA row number (excl. header)
    key: str = ""                     # Business Partner (or best row key)
    bucket: str = "VALID"
    rule_ids: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def hit(self, bucket: str, rule_id: str, reason: str) -> None:
        """Record a check hit; the row's bucket is the WORST hit (INVALID >
        SUSPICIOUS > VALID > SKIPPED)."""
        if rule_id and rule_id not in self.rule_ids:
            self.rule_ids.append(rule_id)
        if reason:
            self.reasons.append(reason)
        if _RANK.get(bucket, 0) > _RANK.get(self.bucket, 0):
            self.bucket = bucket

    def skip(self, rule_id: str, reason: str) -> None:
        """Mark a row that cannot be judged — only if nothing worse was hit."""
        if rule_id not in self.rule_ids:
            self.rule_ids.append(rule_id)
        self.reasons.append(reason)
        if self.bucket == "VALID":
            self.bucket = "SKIPPED"


@dataclass
class BulkResult:
    case: str                         # bank | tax | region
    source_file: str = ""
    source_kind: str = ""             # template | raw:<marker>
    rows: list = field(default_factory=list)     # [RowVerdict]
    columns: list = field(default_factory=list)  # canonical input columns seen
    options: dict = field(default_factory=dict)  # web=..., refs=[...]
    notes: list = field(default_factory=list)    # run-level notes (refs used…)

    def counts(self) -> dict:
        c = {b: 0 for b in BUCKETS}
        for r in self.rows:
            c[r.bucket] = c.get(r.bucket, 0) + 1
        return c

    def top_reasons(self, n: int = 12) -> list:
        freq: dict[str, int] = {}
        for r in self.rows:
            for rid in r.rule_ids:
                freq[rid] = freq.get(rid, 0) + 1
        return sorted(freq.items(), key=lambda kv: -kv[1])[:n]

    def summary(self) -> dict:
        return {"case": self.case, "source_file": self.source_file,
                "source_kind": self.source_kind, "total": len(self.rows),
                "counts": self.counts(),
                "top_rules": [{"rule_id": k, "rows": v}
                              for k, v in self.top_reasons()],
                "options": self.options, "notes": self.notes,
                "columns": self.columns}
