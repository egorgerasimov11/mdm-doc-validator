#!/usr/bin/env python3
"""
privacy.py — the single choke point for sensitive values.

Full SSN/TIN/EIN, IBANs and account numbers exist ONLY in memory during a run.
Everything persisted or printed flows through mask()/scrub_text() and is gated
by assert_no_leak(). Few-shot / LoRA exemplars use fake_preserve_shape() values
(same prefix/length/hyphenation, different digits) so nothing real ever leaves.
"""
from __future__ import annotations

import hashlib
import re

from . import ocr

SENSITIVE_KINDS = ("ssn", "ein", "tin", "iban", "account_number", "routing_aba")

# sensitive field name -> kind (used by callers to route through mask())
FIELD_KIND = {
    "iban": "iban",
    "account_number": "account_number",
    "routing_aba": "routing_aba",
    "tin_raw": "tin",
    "tin_boxed": "tin",
    "ein": "ein",
    "ssn": "ssn",
}


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def mask(kind: str, value: str) -> str:
    """Masked display form. Hyphen style of SSN/EIN is preserved (rule DR-20260608-121850)."""
    v = (value or "").strip()
    if not v:
        return ""
    d = _digits(v)
    if kind == "ssn":
        return "XXX-XX-" + d[-4:] if len(d) >= 4 else "XXX-XX-????"
    if kind == "ein":
        return "XX-XXX" + d[-4:] if len(d) >= 4 else "XX-XXX????"
    if kind == "tin":
        # route by shape: 3-2-4 hyphens = SSN style, 2-7 = EIN style
        if re.match(r"^\d{3}-\d{2}-\d{4}$", v):
            return mask("ssn", v)
        if re.match(r"^\d{2}-\d{7}$", v):
            return mask("ein", v)
        return ("*" * max(len(d) - 4, 2)) + d[-4:] if len(d) >= 5 else "****"
    if kind == "iban":
        c = re.sub(r"\s", "", v).upper()
        if len(c) >= 8:
            return f"{c[:2]}**…{c[-4:]}"
        return c[:2] + "**…"
    if kind in ("account_number", "routing_aba"):
        return "…" + d[-4:] if len(d) >= 4 else "…" + d
    # unknown kind: safest generic
    return ("*" * max(len(d) - 4, 2)) + d[-4:] if len(d) >= 5 else "****"


def fake_preserve_shape(kind: str, value: str, seed: str = "mdmdoc") -> str:
    """Deterministic fake with identical shape: keeps letters, hyphens, spaces and
    length; replaces digits (except an IBAN's 2-letter prefix stays, its digits change).
    Used for few-shot / LoRA exemplars so training data contains no real values."""
    v = (value or "").strip()
    if not v:
        return ""
    stream = hashlib.sha256((seed + "|" + kind + "|" + v).encode()).hexdigest()
    digits = [str(int(c, 16) % 10) for c in stream] * 4
    out, di = [], 0
    for ch in v:
        if ch.isdigit():
            out.append(digits[di])
            di += 1
        else:
            out.append(ch)
    return "".join(out)


class SecretVault:
    """Collects the full sensitive values seen during one run, so outputs can be
    scanned for exact leaks and few-shot exports can swap masked->fake consistently."""

    def __init__(self) -> None:
        self._items: list[dict] = []   # {kind, value, masked, fake}

    def register(self, kind: str, value: str) -> str:
        v = (value or "").strip()
        if not v or len(_digits(v)) < 4:
            return v
        for it in self._items:
            if it["value"] == v:
                return it["masked"]
        it = {"kind": kind, "value": v, "masked": mask(kind, v),
              "fake": fake_preserve_shape(kind, v)}
        self._items.append(it)
        return it["masked"]

    def items(self) -> list[dict]:
        return list(self._items)

    def secrets(self) -> list[str]:
        return [it["value"] for it in self._items]

    def sensitive_map(self) -> list[dict]:
        """Masked<->fake pairs (NO real values) — persisted into labels for LoRA export."""
        return [{"kind": it["kind"], "masked": it["masked"], "fake": it["fake"]}
                for it in self._items]


# --- free-text scrubbing ----------------------------------------------------
_ACCT_NEAR_RE = re.compile(
    r"(?i)((?:acc(?:oun)?t|iban|계좌|口座|账[户戶号號]|帐[户戶号號])[^0-9]{0,14})([0-9][0-9 \-]{5,20}[0-9])")
_LONG_DIGITS_RE = re.compile(r"\b\d[\d \-]{9,20}\d\b")


def scrub_text(text: str, vault: SecretVault | None = None) -> str:
    """Replace sensitive patterns in free text with masked forms. Applied to ANY
    free text that gets persisted (raw_text excerpts, report evidence lines)."""
    t = text or ""
    # exact known values first (also catches spaced/hyphenated variants via digits)
    if vault:
        for it in vault.items():
            t = t.replace(it["value"], it["masked"])
            dv = _digits(it["value"])
            if len(dv) >= 6:
                # spaced / grouped / one-digit-per-line variants (W-9 boxes):
                # match the digit sequence with any separators between digits
                pat = re.compile(r"[\s\-]*".join(re.escape(c) for c in dv))
                t = pat.sub(it["masked"], t)
    t = ocr.IBAN_RE.sub(lambda m: mask("iban", m.group(1)), t)
    t = ocr.SSN_RE.sub(lambda m: mask("ssn", m.group(1)), t)
    t = ocr.EIN_RE.sub(lambda m: mask("ein", m.group(1)), t)
    t = _ACCT_NEAR_RE.sub(lambda m: m.group(1) + mask("account_number", m.group(2)), t)
    t = _LONG_DIGITS_RE.sub(lambda m: mask("account_number", m.group(0)), t)
    return t


# --- leakage gate -------------------------------------------------------------
_GENERIC_LEAK_RES = (
    ocr.SSN_RE,
    ocr.EIN_RE,
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),   # full unmasked IBAN
    re.compile(r"\b\d{11,18}\b"),                       # long bare digit run
)


def assert_no_leak(blob: str, known_secrets: list[str] | None = None,
                   allowed_fakes: list[str] | None = None,
                   raise_on_hit: bool = True) -> list[str]:
    """Scan an output string for full sensitive values. Returns list of leak
    descriptions; raises ValueError when raise_on_hit and something leaked.

    allowed_fakes: shape-preserving fakes (from sensitive_map) are SUPPOSED to
    look real, so generic-pattern hits equal to a registered fake are skipped.
    Real values are still caught by the known-secret pass, which runs first."""
    leaks: list[str] = []
    b = blob or ""
    b_norm = re.sub(r"[\s\-]", "", b)
    fake_norms = {re.sub(r"[\s\-]", "", f) for f in (allowed_fakes or [])}
    for s in known_secrets or []:
        s_norm = re.sub(r"[\s\-]", "", s)
        if len(_digits(s_norm)) < 6:
            continue
        if s in b or (s_norm and s_norm in b_norm):
            leaks.append(f"known-secret:{mask('tin', s)}")
    for rx in _GENERIC_LEAK_RES:
        for m in rx.finditer(b):
            if re.sub(r"[\s\-]", "", m.group(0)) in fake_norms:
                continue
            leaks.append(f"pattern:{rx.pattern[:40]}:{mask('account_number', m.group(0))}")
            break
    if leaks and raise_on_hit:
        raise ValueError(f"sensitive value leak blocked: {leaks}")
    return leaks
