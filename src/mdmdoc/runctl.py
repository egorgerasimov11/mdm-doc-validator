#!/usr/bin/env python3
"""
runctl.py — the per-run control plane (D1): cooperative cancellation, stage
progress and per-run config overrides, carried through the pipeline WITHOUT
touching deep function signatures and WITHOUT process-global state.

Why a ContextVar and not env mutation or kwargs plumbing: every server job is
its own thread and a fresh thread starts with an empty context, so a control
activated inside one job can never leak into a concurrently queued check, an
eval or the CLI. `os.environ` writes would race the moment two runs coexist.
Deep call sites already read their knobs through `config.*()` functions — each
of those gets a one-line override seam instead of a new parameter.

Usage (pipeline.run_check):
    ctl = RunControl(cancel=..., on_stage=..., overrides={...})
    token = activate(ctl)
    try:
        checkpoint("perception", 8)   # report + raise CheckCanceled if canceled
        ...
    finally:
        deactivate(token)
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable


class CheckCanceled(RuntimeError):
    """The operator canceled the run — cooperative, raised at checkpoints."""


@dataclass
class RunControl:
    cancel: threading.Event | None = None
    on_stage: Callable[[str, int], None] | None = None   # (stage, percent)
    overrides: dict = field(default_factory=dict)


CURRENT: ContextVar[RunControl | None] = ContextVar("mdmdoc_runctl", default=None)

_LAST_PCT = ContextVar("mdmdoc_runctl_pct", default=0)


def activate(ctl: RunControl):
    _LAST_PCT.set(0)
    return CURRENT.set(ctl)


def deactivate(token) -> None:
    CURRENT.reset(token)


def checkpoint(stage: str, pct: int | None = None) -> None:
    """Report a pipeline stage and honour cancellation. Percent is monotonic:
    a checkpoint without one (mid-stage vision loops) re-reports the last."""
    ctl = CURRENT.get()
    if ctl is None:
        return
    if ctl.cancel is not None and ctl.cancel.is_set():
        raise CheckCanceled(f"canceled at stage '{stage}'")
    if pct is not None and pct > _LAST_PCT.get():
        _LAST_PCT.set(pct)
    if ctl.on_stage is not None:
        try:
            ctl.on_stage(stage, _LAST_PCT.get())
        except Exception:
            pass   # progress reporting must never break a run


def bail_if_canceled(stage: str = "model") -> None:
    """Lightweight cancel check for HOT loops (called per streamed model chunk).
    Unlike checkpoint() it never touches progress/on_stage, so it is cheap to
    call thousands of times. NO-OP when no control is active OR no cancel Event
    is wired (eval / CLI / tests) — so a streaming model call only pays the
    check where a cancel Event actually exists (a server job)."""
    ctl = CURRENT.get()
    if ctl is None or ctl.cancel is None:
        return
    if ctl.cancel.is_set():
        raise CheckCanceled(f"canceled during '{stage}'")


def cancellation_active() -> bool:
    """True iff a control with a LIVE cancel Event is bound to this thread — a
    server job the operator can cancel. False for eval / CLI / tests, which is
    how model_client decides to take the plain (non-streaming) transport."""
    ctl = CURRENT.get()
    return ctl is not None and ctl.cancel is not None


def current_cancel() -> "threading.Event | None":
    """The live cancel Event for this run, or None. Lets a blocking transport
    (a streamed model call waiting on the first token) watch cancellation
    directly instead of only between NDJSON lines."""
    ctl = CURRENT.get()
    return ctl.cancel if ctl is not None else None


def override(key: str, default=None):
    """Per-run config override: run override > (caller falls back to env/default).
    Returns `default` when no control is active or the key is absent."""
    ctl = CURRENT.get()
    if ctl is None:
        return default
    return ctl.overrides.get(key, default)
