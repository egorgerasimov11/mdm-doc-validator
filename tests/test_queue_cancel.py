"""D2: the FIFO pipeline gate + cooperative cancel — order is fair, a queued
cancel never runs, a running cancel stops at the next checkpoint, legacy
PIPELINE_LOCK users still exclude gated jobs."""
import threading
import time

import pytest

from mdmdoc import runctl
from mdmdoc.server import jobs


def _job(jid="j1"):
    return jobs.Job(id=jid, kind="check", created="t")


def test_fifo_order_three_workers():
    gate = jobs.PipelineGate(threading.Lock())
    done: list[str] = []
    started = threading.Barrier(4)

    def worker(name, delay):
        j = _job(name)
        started.wait()
        time.sleep(delay)                       # stagger arrival: a < b < c
        with gate.slot(j):
            done.append(name)
            time.sleep(0.05)

    ts = [threading.Thread(target=worker, args=(n, d))
          for n, d in (("a", 0.0), ("b", 0.1), ("c", 0.2))]
    for t in ts:
        t.start()
    started.wait()
    for t in ts:
        t.join(timeout=10)
    assert done == ["a", "b", "c"]


def test_cancel_while_queued_never_runs():
    lock = threading.Lock()
    gate = jobs.PipelineGate(lock)
    blocker = _job("holder")
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with gate.slot(blocker):
            entered.set()
            release.wait(timeout=10)

    t1 = threading.Thread(target=hold)
    t1.start()
    entered.wait(timeout=5)

    waiting = _job("victim")
    result: dict = {}

    def wait_in_queue():
        try:
            with gate.slot(waiting):
                result["ran"] = True
        except runctl.CheckCanceled:
            result["canceled"] = True

    t2 = threading.Thread(target=wait_in_queue)
    t2.start()
    time.sleep(0.3)                              # victim is queued behind holder
    assert gate.position("victim") == 1
    waiting.cancel.set()
    t2.join(timeout=5)
    assert result == {"canceled": True}
    release.set()
    t1.join(timeout=5)
    assert gate.position("victim") is None       # ticket cleaned up


def test_running_cancel_stops_at_checkpoint():
    j = _job("run1")
    token = runctl.activate(runctl.RunControl(cancel=j.cancel))
    try:
        runctl.checkpoint("extraction", 40)      # fine
        j.cancel.set()
        with pytest.raises(runctl.CheckCanceled):
            runctl.checkpoint("rules", 76)
    finally:
        runctl.deactivate(token)


def test_legacy_lock_users_exclude_gate():
    lock = threading.Lock()
    gate = jobs.PipelineGate(lock)
    order: list[str] = []
    with lock:                                    # legacy `with PIPELINE_LOCK:`
        t = threading.Thread(target=lambda: (gate.slot(None).__enter__(),
                                             order.append("gated"),
                                             lock.release()))
        t.start()
        time.sleep(0.4)
        assert order == []                        # gated job blocked by legacy holder
        order.append("legacy")
    t.join(timeout=5)
    assert order == ["legacy", "gated"]


def test_cancel_endpoint_codes():
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/jobs/nope/cancel")
    assert r.status_code == 404
    j = jobs.REGISTRY.submit("eval", lambda log: {})
    time.sleep(0.1)
    r = client.post(f"/api/v1/jobs/{j.id}/cancel")
    assert r.status_code == 409                   # non-check kind

    slow = threading.Event()

    def work(log, job):
        slow.wait(timeout=5)
        return {}

    jc = jobs.REGISTRY.submit("check", work, pass_job=True)
    r = client.post(f"/api/v1/jobs/{jc.id}/cancel")
    assert r.status_code == 200 and jc.cancel.is_set()
    slow.set()
    r = client.post(f"/api/v1/jobs/{j.id}/cancel")
    assert r.status_code in (409,)                # done/error by now -> conflict


def test_job_dict_carries_queue_fields():
    j = jobs.REGISTRY.submit("check", lambda log, job: {}, pass_job=True)
    d = j.to_dict()
    for k in ("stage", "percent", "estimate_s", "label", "queue_pos", "cancelable"):
        assert k in d


def test_study_and_retrain_are_cancelable_kinds():
    """H2: the study button used to 409 (endpoint allowed only check/bulk while
    the button rendered for study). One CANCELABLE_KINDS source of truth now."""
    import threading
    import time

    from fastapi.testclient import TestClient

    from mdmdoc.server import jobs
    from mdmdoc.server.app import create_app
    assert {"check", "bulk", "study", "retrain"} <= jobs.CANCELABLE_KINDS
    client = TestClient(create_app("full"))
    slow = threading.Event()

    def work(log, job):
        slow.wait(timeout=5)
        return {}

    j = jobs.REGISTRY.submit("study", work, pass_job=True)
    time.sleep(0.05)
    assert j.to_dict()["cancelable"] is True
    r = client.post(f"/api/v1/jobs/{j.id}/cancel")
    assert r.status_code == 200 and j.cancel.is_set()
    slow.set()
