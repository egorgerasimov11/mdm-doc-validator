#!/usr/bin/env python3
"""
web_enrichment — an EVIDENCE layer, never a decision layer.

It corroborates a document's public identifiers against external registries
(FDIC, Federal Reserve routing, SWIFT/BIC syntax, GLEIF, SEC EDGAR) and returns
NOTE-tier hints only. The rule engine + YAML rules remain the sole deciders
(project invariant #1). The UI shows a permanent banner: "web did not decide
this verdict."

Guarantees:
  * OPT-IN     — off unless MDMDOC_WEB_EVIDENCE=1 (unset in the BTP image).
  * PRIVACY    — every outbound query passes egress.assert_safe_outbound; only
                 routing/ABA, SWIFT/BIC, bank names and company names ever leave
                 the machine (never TIN/SSN, account numbers or IBANs).
  * FAIL-SOFT  — offline / unreachable / connector error degrades to an
                 'unavailable' hint; it never crashes a run or moves a verdict.
  * NEVER DECIDES — every emitted Finding is severity=NOTE, verdict_effect=None.
"""
from __future__ import annotations

import os

from . import aba, egress, entity, swift
from .evidence import UNAVAILABLE, Evidence

BANNER = ("Web did not decide this verdict. These rows are advisory hints from "
          "external sources; the verdict comes only from the document and the rules.")

_PROVIDERS = (aba, swift, entity)


def enabled() -> bool:
    """Network evidence is strictly opt-in. Default OFF (and off in the BTP image,
    which never sets the flag) so the sealed image makes no outbound calls."""
    return os.environ.get("MDMDOC_WEB_EVIDENCE", "").strip().lower() in ("1", "true", "on", "yes")


def gather(ext, policy: str = "masked") -> tuple[list, list[dict]]:
    """Run every connector and return (findings, rows).

    Returns ([], []) when disabled. Each connector is isolated: an EgressBlocked
    (a would-be leak, caught by the guard — nothing sent) or any other error
    becomes a visible 'unavailable' hint instead of failing the document run."""
    if not enabled():
        return [], []
    evidence: list[Evidence] = []
    for mod in _PROVIDERS:
        name = mod.__name__.rsplit(".", 1)[-1]
        try:
            evidence.extend(mod.collect(ext))
        except egress.EgressBlocked as e:
            evidence.append(Evidence(
                f"{name}_egress_block", UNAVAILABLE,
                "a query was blocked by the outbound privacy guard — nothing was sent",
                "egress guard", 1, "WEB-EGRESS-1", detail=str(e)))
        except Exception as e:  # a flaky connector must not sink the whole run
            evidence.append(Evidence(
                f"{name}_error", UNAVAILABLE, f"{name} connector error",
                name, 1, "WEB-ERR-1", detail=f"{e.__class__.__name__}: {e}"))
    findings = [e.to_finding() for e in evidence]
    # Belt-and-suspenders: the layer can NEVER contribute a verdict effect.
    findings = [f for f in findings if f.severity == "NOTE" and f.verdict_effect is None]
    rows = [e.to_row() for e in evidence]
    return findings, rows
