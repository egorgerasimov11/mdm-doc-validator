"""D1: the per-run control plane — cooperative cancel, stage progress and
per-run config overrides carried in a ContextVar (thread-isolated, no env)."""
import threading

import fitz
import pytest

from mdmdoc import config, runctl
from mdmdoc.pipeline import run_check
from mdmdoc.runctl import CheckCanceled, RunControl


def test_override_precedence(monkeypatch):
    monkeypatch.setenv("MDMDOC_SIG_VISION_CAP", "7")
    assert config.sig_vision_cap() == 7            # env wins with no control
    token = runctl.activate(RunControl(overrides={"sig_vision_cap": 2}))
    try:
        assert config.sig_vision_cap() == 2        # run override wins over env
        assert config.ladder_pages() == 2          # untouched knob -> env/default
    finally:
        runctl.deactivate(token)
    assert config.sig_vision_cap() == 7            # deactivation restores


def test_override_is_thread_isolated():
    token = runctl.activate(RunControl(overrides={"ladder_pages": 9}))
    seen = {}

    def other():
        seen["pages"] = config.ladder_pages()      # fresh thread: no control

    try:
        assert config.ladder_pages() == 9
        t = threading.Thread(target=other)
        t.start()
        t.join()
    finally:
        runctl.deactivate(token)
    assert seen["pages"] == 2


def test_checkpoint_cancel_and_progress():
    ev = threading.Event()
    stages = []
    token = runctl.activate(RunControl(
        cancel=ev, on_stage=lambda s, p: stages.append((s, p))))
    try:
        runctl.checkpoint("perception", 8)
        runctl.checkpoint("signature")             # no pct -> keeps last
        assert stages == [("perception", 8), ("signature", 8)]
        ev.set()
        with pytest.raises(CheckCanceled):
            runctl.checkpoint("extraction", 40)
    finally:
        runctl.deactivate(token)


def test_checkpoint_noop_without_control():
    runctl.checkpoint("anything", 50)              # must not raise


def test_canceled_run_writes_no_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    p = tmp_path / "doc.pdf"
    d = fitz.open()
    d.new_page().insert_text((72, 100), "Bank confirmation letter. IBAN DE89")
    d.save(p)
    d.close()
    ev = threading.Event()
    ev.set()                                       # canceled before it starts
    with pytest.raises(CheckCanceled):
        run_check(p, "bank", use_vision=False, engine="deterministic", cancel=ev)
    run_dirs = [x for x in (tmp_path / "runs").glob("*/meta.json")]
    assert run_dirs == []
