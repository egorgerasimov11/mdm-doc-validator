#!/usr/bin/env python3
"""
jobs.py — in-process background jobs for the single-operator server.

- Daemon threads + a registry dict; no external queue.
- PIPELINE_LOCK serializes every run_check: the mini runs with
  OLLAMA_MAX_LOADED_MODELS=1, concurrent pipelines would thrash model loads.
- A thread-routing stdout proxy captures print() output of engine functions
  (fewshot/modelfile/lora) into the calling job's log without touching the
  engine or other threads.
"""
from __future__ import annotations

import collections
import logging
import sys
import threading
import uuid
from dataclasses import dataclass, field

from .. import runstore
from ..privacy import scrub_text

PIPELINE_LOCK = threading.Lock()

# --- ring buffer for the debug page (paths/run_ids only — never bodies) -------
LOG_RING: collections.deque = collections.deque(maxlen=500)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_RING.append(f"{self.format(record)}")
        except Exception:
            pass


# --- thread-routing stdout proxy ----------------------------------------------
class _StdoutRouter:
    """sys.stdout replacement: writes go to a per-thread sink when one is set,
    else fall through to the real stdout. Thread-correct, unlike
    contextlib.redirect_stdout which is process-global."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def set_sink(self, sink) -> None:
        self._local.sink = sink

    def clear_sink(self) -> None:
        self._local.sink = None

    def write(self, s):
        sink = getattr(self._local, "sink", None)
        if sink is not None:
            sink(s)
            return len(s)
        return self._real.write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


_ROUTER: _StdoutRouter | None = None


def install_stdout_router() -> _StdoutRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = _StdoutRouter(sys.stdout)
        sys.stdout = _ROUTER
    return _ROUTER


# --- pipeline gate: FIFO queue over the single pipeline slot (D2) --------------
class PipelineGate:
    """Fair FIFO admission to the pipeline. Built ON PIPELINE_LOCK so legacy
    `with jobs.PIPELINE_LOCK:` users (retrain/propose/eval/adoption) keep strict
    mutual exclusion with gated check jobs. While a ticket WAITS its job stays
    'queued' with a visible position; waiting is interruptible by the job's
    cancel event (a queued cancel never runs the pipeline at all)."""

    def __init__(self, lock: threading.Lock):
        self._lock = lock
        self._cond = threading.Condition()
        self._tickets: collections.deque[str] = collections.deque()

    def position(self, ticket: str) -> int | None:
        """0 = next in line (or holding the slot); None = not queued."""
        with self._cond:
            try:
                return list(self._tickets).index(ticket)
            except ValueError:
                return None

    class _Slot:
        def __init__(self, gate: "PipelineGate", job):
            self._gate, self._job = gate, job
            self._ticket = (job.id if job is not None else uuid.uuid4().hex[:12])
            self._acquired = False

        def __enter__(self):
            g = self._gate
            with g._cond:
                g._tickets.append(self._ticket)
            try:
                while True:
                    if (self._job is not None and self._job.cancel.is_set()):
                        from .. import runctl
                        raise runctl.CheckCanceled("canceled while queued")
                    with g._cond:
                        at_head = g._tickets and g._tickets[0] == self._ticket
                    # only the head ticket may contend for the real lock —
                    # Lock wakeups are unordered, the deque supplies fairness
                    if at_head and g._lock.acquire(timeout=0.25):
                        self._acquired = True
                        return self
                    if not at_head:
                        with g._cond:
                            g._cond.wait(timeout=0.25)
            except BaseException:
                self._release_ticket()
                raise

        def __exit__(self, *exc):
            if self._acquired:
                self._gate._lock.release()
            self._release_ticket()
            return False

        def _release_ticket(self):
            g = self._gate
            with g._cond:
                try:
                    g._tickets.remove(self._ticket)
                except ValueError:
                    pass
                g._cond.notify_all()

    def slot(self, job=None) -> "PipelineGate._Slot":
        return PipelineGate._Slot(self, job)


# --- job registry ---------------------------------------------------------------
# the ONE source of truth for what the operator can cancel — used by both
# Job.to_dict (renders the button) and the cancel endpoint (honors it). A kind
# is cancelable only if its worker actually observes job.cancel: `check`/`bulk`
# at pipeline/row checkpoints + streamed model calls, `study` between documents,
# `retrain` between its stages. (A study button that 409'd was the mismatch.)
CANCELABLE_KINDS = frozenset({"check", "bulk", "study", "retrain"})


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"            # queued | running | done | error | canceled
    progress: list = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    created: str = ""
    started: str | None = None
    finished: str | None = None
    # D2/D3: live progress + cancellation
    stage: str = ""
    percent: int = 0
    estimate_s: int | None = None
    label: str = ""                   # file name shown in the queue panel
    cancel: threading.Event = field(default_factory=threading.Event)

    def to_dict(self, after: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "progress": self.progress[after:], "progress_len": len(self.progress),
                "result": self.result, "error": self.error,
                "created": self.created, "started": self.started, "finished": self.finished,
                "stage": self.stage, "percent": self.percent,
                "estimate_s": self.estimate_s, "label": self.label,
                "queue_pos": GATE.position(self.id),
                "cancelable": self.kind in CANCELABLE_KINDS
                and self.status in ("queued", "running")}


class JobRegistry:
    def __init__(self, cap: int = 50):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._cap = cap

    def submit(self, kind: str, fn, capture_stdout: bool = False,
               pass_job: bool = False) -> Job:
        """fn(log) -> dict result; with pass_job=True fn(log, job) — the worker
        hands its own Job in BEFORE the thread starts (no set-after-start race
        for cancel/progress wiring)."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, created=runstore.now_iso())
        from .. import oplog
        oplog.log("job-start", job_id=job.id, detail=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._cap:
                old = self._order.pop(0)
                if self._jobs.get(old) and self._jobs[old].status in ("done", "error",
                                                                      "canceled"):
                    self._jobs.pop(old, None)

        def log(line: str) -> None:
            with self._lock:
                job.progress.append(line.rstrip("\n"))

        def worker() -> None:
            job.status = "running"
            job.started = runstore.now_iso()
            router = _ROUTER
            if capture_stdout and router:
                buf: list[str] = []

                def sink(s: str) -> None:
                    buf.append(s)
                    if "\n" in s:
                        text = "".join(buf)
                        buf.clear()
                        for ln in text.splitlines():
                            if ln.strip():
                                log(ln)
                router.set_sink(sink)
            try:
                job.result = (fn(log, job) if pass_job else fn(log)) or {}
                job.status = "done"
            except Exception as e:  # noqa: BLE001 — job errors are data
                from .. import runctl
                if isinstance(e, runctl.CheckCanceled):
                    job.status = "canceled"
                    log("canceled by operator")
                else:
                    job.error = scrub_text(f"{e.__class__.__name__}: {e}")
                    job.status = "error"
            finally:
                if capture_stdout and router:
                    router.clear_sink()
                job.finished = runstore.now_iso()
                from .. import oplog
                dur = ""
                try:
                    import datetime as _dt
                    dur = str(int((_dt.datetime.fromisoformat(job.finished.rstrip("Z"))
                                   - _dt.datetime.fromisoformat((job.started or job.created).rstrip("Z"))
                                   ).total_seconds()))
                except Exception:
                    pass
                oplog.log("job-end", job_id=job.id,
                          detail=f"{kind} {job.status}"
                                 + (f" {dur}s" if dur else "")
                                 + (f" — {job.label}" if job.label else ""))

        threading.Thread(target=worker, daemon=True, name=f"job-{kind}-{job.id}").start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def running(self, kinds: set[str] | None = None) -> Job | None:
        for j in self._jobs.values():
            if j.status in ("queued", "running") and (kinds is None or j.kind in kinds):
                return j
        return None

    def cancel(self, job_id: str) -> str:
        """Set the cancel flag; returns the job's CURRENT status ('' unknown).
        A queued job dies at the gate; a running one at its next checkpoint."""
        j = self._jobs.get(job_id)
        if j is None:
            return ""
        j.cancel.set()
        return j.status


REGISTRY = JobRegistry()
GATE = PipelineGate(PIPELINE_LOCK)
