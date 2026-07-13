#!/usr/bin/env python3
"""
app.py — FastAPI application factory.

Modes (arg or MDMDOC_MODE env):
  full      — operator console: UI pages + full API (default, 127.0.0.1)
  api-only  — BTP/headless: core API only; no UI, no teach/train/eval routes,
              so the served OpenAPI is honest about the deployed surface.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from .. import __version__, config
from ..privacy import scrub_text

PKG_DIR = Path(__file__).resolve().parent


import re as _re

_AUDIT_ROUTES = (
    (_re.compile(r"^/api/v1/rules/skill-upload$"), "skill-upload", ()),
    (_re.compile(r"^/api/v1/rules/unified$"), "rule-save", ()),
    (_re.compile(r"^/api/v1/rules/(?P<doc_class>\w+)/raw$"), "rule-save", ()),
    (_re.compile(r"^/api/v1/rules/regenerate$"), "rule-regenerate", ()),
    (_re.compile(r"^/api/v1/rules/(?P<doc_class>\w+)/delete$"), None, ()),   # handler logs (rule_id)
    (_re.compile(r"^/api/v1/rules/(?P<doc_class>\w+)/approve$"), None, ()),  # handler logs (rule_id)
    (_re.compile(r"^/api/v1/rules/(?P<doc_class>\w+)/tier$"), None, ()),     # handler logs (rule_id)
    (_re.compile(r"^/api/v1/rules/(?P<doc_class>\w+)/create$"), None, ()),   # handler logs (rule-create)
    (_re.compile(r"^/api/v1/rules/(?P<doc_class>\w+)/draft$"), None, ()),    # job rows cover the draft
    (_re.compile(r"^/api/v1/settings$"), "settings", ()),
    (_re.compile(r"^/api/v1/tags$"), "tag", ()),
    (_re.compile(r"^/api/v1/tags/assign$"), "tag", ()),
    (_re.compile(r"^/api/v1/check$"), "check", ()),
    (_re.compile(r"^/api/v1/check-routing$"), "check", ()),
    (_re.compile(r"^/api/v1/bulk$"), "bulk", ()),
    (_re.compile(r"^/api/v1/jobs/(?P<job_id>\w+)/cancel$"), "cancel", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/label$"), "label", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/mark-valid$"), "mark-valid", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/revise-verdict$"), "revise-verdict", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/confirm-gold$"), "confirm-gold", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/rating$"), None, ()),    # handler logs (detail=rating)
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/test$"), "run-test", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/propose-fix$"), "propose", ()),
    (_re.compile(r"^/api/v1/runs/(?P<run_id>\w+)/findings/(?P<rule_id>[\w-]+)/vote$"),
     None, ()),                                      # handler logs (rule_id + detail)
    (_re.compile(r"^/api/v1/train/(?P<detail>[\w-]+)$"), "train", ()),
    (_re.compile(r"^/api/v1/eval$"), "eval", ()),
)


def _audit_route(path: str):
    """-> (action|None, kwargs) for the audit middleware."""
    for rx, action, _ in _AUDIT_ROUTES:
        m = rx.match(path)
        if m:
            return action, {k: v for k, v in m.groupdict().items() if v}
    return None, {}


def create_app(mode: str | None = None) -> FastAPI:
    mode = mode or os.environ.get("MDMDOC_MODE", "full")
    os.environ["MDMDOC_MODE"] = mode
    config.ensure_dirs()

    from . import jobs
    jobs.install_stdout_router()
    handler = jobs.RingHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                           datefmt="%H:%M:%S"))
    for name in ("mdmdoc", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(handler)

    app = FastAPI(title="mdmdoc API", version=__version__,
                  description="Local MDM Document Validator — banking documents and "
                              "W-9 forms. Model classifies/extracts; explicit YAML "
                              "rules decide verdicts. Sensitive values are masked "
                              "everywhere.",
                  openapi_tags=[
                      {"name": "check", "description": "Validate a document"},
                      {"name": "runs", "description": "Past runs and artifacts"},
                      {"name": "jobs", "description": "Background job polling"},
                      {"name": "system", "description": "Health, doctor, rules"},
                      {"name": "teach", "description": "Operator-only teach loop "
                                                       "(absent in api-only mode)"},
                  ])

    @app.get("/health", tags=["system"])
    def health() -> dict:
        # liveness only — never probes the model host (Docker HEALTHCHECK target)
        return {"status": "ok", "version": __version__, "mode": mode}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "error", "message": scrub_text(str(exc.detail))}
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def any_exc(request: Request, exc: Exception):
        return JSONResponse(
            {"error": {"code": "internal",
                       "message": scrub_text(f"{exc.__class__.__name__}: {exc}")}},
            status_code=500)

    from .api import router_core, router_teach
    app.include_router(router_core)

    if mode != "api-only":
        app.include_router(router_teach)
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
        from .ui import router_ui
        app.include_router(router_ui)

        # optional address-validator tab (separate project; same URL when installed).
        # app.state.addr_enabled drives the nav entry — a link to an unmounted
        # sub-app is a dead link, so the tab only exists when the mount succeeded.
        app.state.addr_enabled = False
        try:
            from addrval.server import create_address_app
            app.mount("/address", create_address_app())
            app.state.addr_enabled = True
        except ImportError:
            logging.getLogger("mdmdoc").info("addrval not installed — /address tab disabled")

        @app.get("/", include_in_schema=False)
        def index():
            return RedirectResponse("/ui")

        @app.middleware("http")
        async def _operator_audit(request: Request, call_next):
            # E1 oplog: ONE seam records every successful mutating operator
            # action (POST /api/v1/*). Handlers with body-level detail
            # (approve/tier: rule_id) log richer rows themselves and are
            # excluded here to avoid doubles.
            resp = await call_next(request)
            try:
                path = request.url.path
                if (request.method == "POST" and path.startswith("/api/v1/")
                        and resp.status_code < 400):
                    from .. import oplog
                    action, kw = _audit_route(path)
                    if action:
                        oplog.log(action, **kw)
            except Exception:
                pass
            return resp

        @app.middleware("http")
        async def _ui_token_cookie(request: Request, call_next):
            # Browser <img>/download requests can't send the Bearer header, so a
            # UI page load drops a same-origin cookie the token-guard also accepts
            # (deps.require_token). Set before sub-resources load → no broken images.
            resp = await call_next(request)
            tok = os.environ.get("MDMDOC_API_TOKEN", "")
            if tok and request.url.path.startswith("/ui"):
                resp.set_cookie("mdmdoc_token", tok, httponly=True,
                                samesite="strict", path="/")
            return resp

    return app
