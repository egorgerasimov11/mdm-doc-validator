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
              apply_precedent: bool = True, quality: bool = False,
              web_evidence: bool | None = None) -> CheckResult:
    """apply_precedent=False is for eval: metrics must measure the MACHINE,
    not the operator's stored answers. quality=True forces the strong tier.
    web_evidence: None -> honour the MDMDOC_WEB_EVIDENCE env flag; True/False
    force it (eval passes False — network calls are non-deterministic)."""
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

    # display/gate policy: banking values full for the operator, TIN always masked
    policy = config.bank_values_policy()
    gate = config.gate_policy()

    # Stage B (trainable extraction, two tiers)
    ext = stage_b.extract(raw, quality=quality, policy=policy)

    # rules -> verdict (+ optional SAP comparison as extra findings)
    findings = run_rules(ext, lang=lang, policy=policy)
    sap_rows: list = []
    if sap_image is not None and doc_class == "bank":
        from . import sap_compare
        sap_image = sap_image.expanduser().resolve()
        sap_fields = sap_compare.read_sap_screen(sap_image, ext.vault)
        if sap_fields:
            sap_findings, sap_rows = sap_compare.compare(ext, sap_fields, policy=policy)
            findings += sap_findings
        else:
            ext.warnings.append("SAP screenshot could not be read — comparison skipped")
    verdict = decide(findings)
    # AFTER sap compare — its values are secrets too. Under the tin-only gate the
    # run artifacts legitimately carry full banking values, so only TIN secrets
    # are enforced there; training-data paths always get the full strict set.
    secrets = ext.vault.tin_secrets() if gate == "tin-only" else ext.vault.secrets()

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
            ext.provenance["doc_type"] = {"source": "precedent", "page": None}

    # External evidence (opt-in): corroborate PUBLIC identifiers against outside
    # registries. Runs AFTER the verdict is decided and only ever yields NOTE
    # findings — it is structurally impossible for the web to move the verdict.
    from . import web_enrichment as webenr
    do_web = web_evidence if web_evidence is not None else webenr.enabled()
    web_rows: list = []
    if do_web:
        web_findings, web_rows = webenr.gather(ext, policy=policy)
        findings += web_findings   # NOTE-only; verdict already decided above

    pub = ext.to_public(policy=policy)
    pub["file_name"] = path.name
    if web_rows:
        pub["web_evidence"] = web_rows
    if precedent:
        pub["operator_precedent"] = {"verdict": verdict, "doc_type": ext.doc_type,
                                     "model_verdict": model_verdict,
                                     "model_doc_type": model_doc_type,
                                     "ts": precedent.get("ts", ""),
                                     "notes": precedent.get("notes", "")}
    if sap_rows:
        pub["sap_compare"] = sap_rows

    from .estimate import shape_key
    report_md = rpt.render_report(pub, findings, verdict, lang=lang)
    meta = {"path": str(path), "file_name": path.name, "doc_class": doc_class,
            "run_id": run_id, "ts": runstore.now_iso(), "model": ext.model_id,
            "use_vision": use_vision, "duration_s": round(time.time() - t0, 1),
            "tier": ext.tier, "escalated_because": ext.escalated_because,
            "has_text_layer": raw.has_text_layer, "quality": quality,
            "signature_pass": bool(raw.signature_probe), "web_evidence": bool(do_web),
            "shape_key": shape_key(doc_class, raw.has_text_layer, use_vision,
                                   sap_image is not None, quality),
            "sap_path": str(sap_image) if sap_image is not None else None}
    report_json = rpt.build_json(pub, findings, verdict, meta)

    runstore.write(run_id, "meta.json", meta, secrets, policy=gate)
    runstore.write(run_id, "stage_a.json", stage_a.to_public(raw, ext.vault, policy=policy),
                   secrets, policy=gate)
    runstore.write(run_id, "extraction.json", pub, secrets, policy=gate)
    runstore.write(run_id, "findings.json", [f.to_dict() for f in findings], secrets,
                   policy=gate)
    runstore.write(run_id, "report.md", report_md, secrets, policy=gate)
    runstore.write(run_id, "report.json", report_json, secrets, policy=gate)
    if sap_rows:
        runstore.write(run_id, "sap_compare.json", sap_rows, secrets, policy=gate)
    if web_rows:
        # Routing/ABA numbers legitimately APPEAR here (they are the check subject
        # and are egress-allowed public identifiers), so they must not be in this
        # artifact's forbidden-secret set — otherwise the strict gate (masked
        # policy) would flag a routing number and crash the run. account/IBAN/TIN
        # are still enforced (both known-secrets and the strict generic patterns).
        web_secrets = ext.vault.secrets(webenr.egress.FORBIDDEN_KINDS)
        runstore.write(run_id, "web_evidence.json", web_rows, web_secrets, policy=gate)
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
