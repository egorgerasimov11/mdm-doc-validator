#!/usr/bin/env python3
"""
egress.py — the OUTBOUND choke point. The inbound gate (privacy.assert_no_leak)
stops sensitive values from being PERSISTED; this gate stops them from being
SENT over the network.

HARD privacy rule (from the task + ROADMAP): full TIN/SSN/EIN, account numbers
and IBANs must NEVER appear in an outbound query. Only these may leave the
machine: routing/ABA numbers, SWIFT/BIC codes, bank names, company names, VAT.

Design: rather than re-implement pattern matching, we reuse the audited
`privacy.assert_no_leak` with a strict policy and a forbidden-secret set built
from the run's vault (account_number + iban + every TIN kind). Routing and SWIFT
are deliberately NOT in the forbidden set — they are allowed egress. The strict
generic patterns (full IBAN, 11-18 digit runs, SSN/EIN shapes) add defence in
depth; note that a 9-digit routing number matches none of them (the leak gate
intentionally carries no bare-9-digit pattern), so allowed identifiers pass.

Every connector routes its outbound query string through `assert_safe_outbound`
BEFORE the request is built. A hit raises EgressBlocked — a blocked send is a
caught bug (mask/omit at the source), never a reason to weaken this gate.
"""
from __future__ import annotations

from urllib.parse import unquote_plus

from ..privacy import BANK_KINDS, TIN_KINDS, assert_no_leak

# Kinds that must never leave the machine. Routing/ABA is a BANK_KIND but is an
# ALLOWED identifier, so it is excluded here explicitly.
FORBIDDEN_KINDS = ("iban", "account_number") + TIN_KINDS
ALLOWED_KINDS = ("routing_aba",)   # + swift/bank name/company name (not vault secrets)

assert "routing_aba" in BANK_KINDS and "routing_aba" not in FORBIDDEN_KINDS


class EgressBlocked(RuntimeError):
    """Raised when an outbound query would carry a forbidden sensitive value."""


def forbidden_secrets(vault) -> list[str]:
    """Real values from this run that must never be sent (exact-match blocklist)."""
    if vault is None:
        return []
    return vault.secrets(FORBIDDEN_KINDS)


def assert_safe_outbound(query: str, vault=None) -> None:
    """Raise EgressBlocked if `query` contains any forbidden sensitive value —
    either an exact known secret from the vault, or a generic account/IBAN/TIN
    pattern. Allowed identifiers (routing/ABA, SWIFT/BIC, names) pass through.

    The query is usually a fully-rendered URL whose params `requests` has already
    percent-encoded (spaces -> %20/+, ':' -> %3A). That encoding would let a
    space-separated secret (e.g. an IBAN written 'DE44 5001 …') slip past both
    the known-secret match and the generic patterns, so we scan the URL-DECODED
    form as well — the decode restores the separators that the leak gate's
    normaliser strips, so a spaced/grouped secret is caught regardless of wire
    encoding."""
    q = query or ""
    decoded = unquote_plus(q)
    blob = q if decoded == q else q + "\n" + decoded
    try:
        assert_no_leak(blob, known_secrets=forbidden_secrets(vault),
                       raise_on_hit=True, policy="strict")
    except ValueError as e:
        raise EgressBlocked(f"outbound query blocked (would leak a sensitive value): {e}") from e


def is_safe_outbound(query: str, vault=None) -> bool:
    try:
        assert_safe_outbound(query, vault)
        return True
    except EgressBlocked:
        return False
