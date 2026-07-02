#!/usr/bin/env python3
"""
compare.py — v2 STUB: compare an extracted document against a vendor template
(.xlsx/.xlsm form). Not implemented in v1 by design (the app is document-only).

The verdict pipeline already accepts extra findings (verdict.decide(findings,
extra_findings)), so a comparer only needs to produce rules.engine.Finding
objects — the char-by-char comparison logic to port lives in the form-validator
(agents/banking.py analyze() and agents/w9.py analyze()).
"""
from __future__ import annotations

from typing import Protocol

from .fields import Extraction
from .rules.engine import Finding


class VendorTemplateComparer(Protocol):
    def compare(self, extraction: Extraction, template: dict) -> list[Finding]:
        """Return findings from comparing document extraction vs the vendor form."""
        ...
