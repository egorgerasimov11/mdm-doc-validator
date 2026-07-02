#!/usr/bin/env python3
"""
pipeline.py — orchestration for one document: Stage A -> Stage B -> rules ->
verdict -> report, with all artifacts persisted under runs/<sha16>/ through the
leak gate. Reused by the CLI and by eval.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config, report as rpt, runstore, stage_a, stage_b
from .rules.engine import run_rules
from .verdict import decide


class CheckResult:
    def __init__(self, run_id: str, pub: dict, findings: list, verdict: str,
                 report_md: str, report_json: str):
        self.run_id = run_id
        self.pub = pub
        self.findings = findings
        self.verdict = verdict
        self.report_md = report_md
        self.report_json = report_json


def run_check(path: Path, doc_class: str, use_vision: bool = True, keep_renders: bool = False,
              lang: str = "en") -> CheckResult:
    t0 = time.time()
    config.ensure_dirs()
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    # run id = content hash (artifacts of a re-run overwrite the same dir)
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    run_id = h.hexdigest()[:16]
    rdir = runstore.render_dir(run_id)

    raw = stage_a.perceive(path, doc_class, rdir, use_vision=use_vision)
    if raw.locked:
        runstore.write(run_id, "meta.json", {"path": str(path), "doc_class": doc_class,
                                             "run_id": run_id, "ts": runstore.now_iso(),
                                             "locked": True})
        raise UnreadableDocument("password-protected PDF — request an unlocked copy")

    # Stage B (trainable extraction)
    ext = stage_b.extract(raw)
    secrets = ext.vault.secrets()

    # rules -> verdict
    findings = run_rules(ext, lang=lang)
    verdict = decide(findings)

    pub = ext.to_public()
    pub["file_name"] = path.name

    report_md = rpt.render_report(pub, findings, verdict, lang=lang)
    meta = {"path": str(path), "file_name": path.name, "doc_class": doc_class,
            "run_id": run_id, "ts": runstore.now_iso(), "model": ext.model_id,
            "use_vision": use_vision, "duration_s": round(time.time() - t0, 1)}
    report_json = rpt.build_json(pub, findings, verdict, meta)

    runstore.write(run_id, "meta.json", meta, secrets)
    runstore.write(run_id, "stage_a.json", stage_a.to_public(raw, ext.vault), secrets)
    runstore.write(run_id, "extraction.json", pub, secrets)
    runstore.write(run_id, "findings.json", [f.to_dict() for f in findings], secrets)
    runstore.write(run_id, "report.md", report_md, secrets)
    runstore.write(run_id, "report.json", report_json, secrets)
    runstore.mark_last(run_id)
    if not keep_renders:
        runstore.cleanup_renders(run_id)
    return CheckResult(run_id, pub, findings, verdict, report_md, report_json)


class UnreadableDocument(RuntimeError):
    pass
