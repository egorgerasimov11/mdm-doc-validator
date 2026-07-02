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
              lang: str = "en", sap_image: Path | None = None,
              apply_precedent: bool = True) -> CheckResult:
    """apply_precedent=False is for eval: metrics must measure the MACHINE,
    not the operator's stored answers."""
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

    # rules -> verdict (+ optional SAP comparison as extra findings)
    findings = run_rules(ext, lang=lang)
    sap_rows: list = []
    if sap_image is not None and doc_class == "bank":
        from . import sap_compare
        sap_image = sap_image.expanduser().resolve()
        sap_fields = sap_compare.read_sap_screen(sap_image, ext.vault)
        if sap_fields:
            sap_findings, sap_rows = sap_compare.compare(ext, sap_fields)
            findings += sap_findings
        else:
            ext.warnings.append("SAP screenshot could not be read — comparison skipped")
    verdict = decide(findings)
    secrets = ext.vault.secrets()   # AFTER sap compare — its values are secrets too

    # operator precedent: a confirmed label for THIS document (by content hash)
    # overrides the machine verdict/doc_type — feedback must stick immediately
    model_verdict, model_doc_type = verdict, ext.doc_type
    precedent = _find_precedent(run_id) if apply_precedent else None
    if precedent:
        from .rules.engine import Finding
        gold_v = precedent.get("verdict_gold") or verdict
        gold_t = precedent.get("doc_type_gold") or ext.doc_type
        if gold_v != verdict or gold_t != ext.doc_type:
            note = f" Operator note: {precedent['notes']}" if precedent.get("notes") else ""
            findings.insert(0, Finding(
                "OPERATOR-1", "NOTE", None,
                f"Operator precedent ({precedent.get('ts', '')}): doc_type={gold_t}, "
                f"verdict={gold_v} — overrides the machine result "
                f"({model_doc_type}/{model_verdict}).{note}"))
            verdict, ext.doc_type = gold_v, gold_t

    pub = ext.to_public()
    pub["file_name"] = path.name
    if precedent:
        pub["operator_precedent"] = {"verdict": verdict, "doc_type": ext.doc_type,
                                     "model_verdict": model_verdict,
                                     "model_doc_type": model_doc_type,
                                     "ts": precedent.get("ts", ""),
                                     "notes": precedent.get("notes", "")}
    if sap_rows:
        pub["sap_compare"] = sap_rows

    report_md = rpt.render_report(pub, findings, verdict, lang=lang)
    meta = {"path": str(path), "file_name": path.name, "doc_class": doc_class,
            "run_id": run_id, "ts": runstore.now_iso(), "model": ext.model_id,
            "use_vision": use_vision, "duration_s": round(time.time() - t0, 1),
            "sap_path": str(sap_image) if sap_image is not None else None}
    report_json = rpt.build_json(pub, findings, verdict, meta)

    runstore.write(run_id, "meta.json", meta, secrets)
    runstore.write(run_id, "stage_a.json", stage_a.to_public(raw, ext.vault), secrets)
    runstore.write(run_id, "extraction.json", pub, secrets)
    runstore.write(run_id, "findings.json", [f.to_dict() for f in findings], secrets)
    runstore.write(run_id, "report.md", report_md, secrets)
    runstore.write(run_id, "report.json", report_json, secrets)
    if sap_rows:
        runstore.write(run_id, "sap_compare.json", sap_rows, secrets)
    runstore.mark_last(run_id)
    if not keep_renders:
        runstore.cleanup_renders(run_id)
    return CheckResult(run_id, pub, findings, verdict, report_md, report_json)


class UnreadableDocument(RuntimeError):
    pass


def _find_precedent(run_id: str) -> dict | None:
    """The confirmed label for this exact document (content hash), if any."""
    from .dataset import load_labels
    for lab in load_labels():
        if lab.get("doc_sha256") == run_id and lab.get("confirmed"):
            return lab
    return None
