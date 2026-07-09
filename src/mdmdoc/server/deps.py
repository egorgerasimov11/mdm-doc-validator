#!/usr/bin/env python3
"""deps.py — auth, host hardening and upload helpers for the server."""
from __future__ import annotations

import hmac
import os
import re
from pathlib import Path

from fastapi import HTTPException, Request

from .. import config


def api_error(status: int, code: str, message: str) -> HTTPException:
    from ..privacy import scrub_text
    return HTTPException(status_code=status,
                         detail={"code": code, "message": scrub_text(message)})


async def require_token(request: Request) -> None:
    """Bearer auth when MDMDOC_API_TOKEN is set (mandatory in BTP); no-op otherwise.
    Also rejects non-local Host headers in full mode (DNS-rebinding hardening)."""
    if os.environ.get("MDMDOC_MODE", "full") != "api-only":
        host = (request.headers.get("host") or "").split(":")[0].lower()
        # DNS-rebinding hardening: only localhost + explicitly allowed hosts.
        # MDMDOC_ALLOWED_HOSTS (comma-separated) lets the operator expose the
        # console on a trusted name (e.g. the tailnet host behind `tailscale serve`).
        extra = {h.strip().lower()
                 for h in os.environ.get("MDMDOC_ALLOWED_HOSTS", "").split(",")
                 if h.strip()}
        if extra and not os.environ.get("MDMDOC_API_TOKEN", ""):
            # An exposed console with no token would leave the approval
            # hard-gate unauthenticated. The Host header is client-controlled,
            # so no localhost carve-out — refuse every gated endpoint (C12).
            raise api_error(503, "misconfigured",
                            "MDMDOC_ALLOWED_HOSTS exposes the console but "
                            "MDMDOC_API_TOKEN is not set — all gated endpoints "
                            "refused; set a token or unset MDMDOC_ALLOWED_HOSTS")
        allowed = {"127.0.0.1", "localhost", "testserver", ""} | extra
        if host not in allowed:
            raise api_error(403, "forbidden_host", f"unexpected Host header {host!r}")
    token = os.environ.get("MDMDOC_API_TOKEN", "")
    if not token:
        return
    auth = request.headers.get("authorization", "")
    given = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    # <img>/download sub-resources and direct navigation can't set the Bearer
    # header — the UI page drops a same-origin cookie (see app.py) so those
    # authenticate too; ?token= is a last-resort fallback.
    if not given:
        given = request.cookies.get("mdmdoc_token", "") or request.query_params.get("token", "")
    if not hmac.compare_digest(given, token):
        raise api_error(401, "unauthorized", "missing or invalid bearer token")


_NAME_SAFE = re.compile(r"[^A-Za-z0-9._ \-]+")


def save_upload(filename: str, data: bytes) -> Path:
    """Store an uploaded document as inbox/<sha16>__<sanitized-name> so that
    labels and eval can re-run it later. Content-addressed: re-uploads reuse."""
    import hashlib
    if not data:
        raise api_error(400, "bad_request", "empty upload")
    sha16 = hashlib.sha256(data).hexdigest()[:16]
    base = _NAME_SAFE.sub("_", Path(filename or "document").name).strip() or "document"
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = config.INBOX_DIR / f"{sha16}__{base}"
    if not path.exists():
        path.write_bytes(data)
    return path
