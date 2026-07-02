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


# --- job registry ---------------------------------------------------------------
@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"            # queued | running | done | error
    progress: list = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    created: str = ""
    started: str | None = None
    finished: str | None = None

    def to_dict(self, after: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "progress": self.progress[after:], "progress_len": len(self.progress),
                "result": self.result, "error": self.error,
                "created": self.created, "started": self.started, "finished": self.finished}


class JobRegistry:
    def __init__(self, cap: int = 50):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._cap = cap

    def submit(self, kind: str, fn, capture_stdout: bool = False) -> Job:
        """fn(log: Callable[[str], None]) -> dict result."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, created=runstore.now_iso())
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._cap:
                old = self._order.pop(0)
                if self._jobs.get(old) and self._jobs[old].status in ("done", "error"):
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
                job.result = fn(log) or {}
                job.status = "done"
            except Exception as e:  # noqa: BLE001 — job errors are data
                job.error = scrub_text(f"{e.__class__.__name__}: {e}")
                job.status = "error"
            finally:
                if capture_stdout and router:
                    router.clear_sink()
                job.finished = runstore.now_iso()

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


REGISTRY = JobRegistry()
