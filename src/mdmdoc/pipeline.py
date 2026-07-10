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
              web_evidence: bool | None = None, enforce_approvals: bool = True,
              engine: str | None = None, sap_bp: str = "",
              cancel=None, on_stage=None, overrides: dict | None = None,
              effort: int | None = None) -> CheckResult:
    """Thin control-plane wrapper (D1): binds a RunControl (cooperative cancel
    event, stage-progress callback, per-run config overrides) to THIS thread's
    context for the duration of the run, then delegates. A canceled run raises
    runctl.CheckCanceled from the next checkpoint and writes NO artifacts
    (every runstore.write sits after the last checkpoint).
    effort (D4): 1..5 resolves an effort profile — it supplies the ENGINE, the
    strong-tier flag and knob overrides, and beats the legacy quality/engine
    params. None/0 keeps legacy behavior exactly (level 3 == today's auto)."""
    from . import runctl
    eff_overrides: dict = {}
    if effort:
        prof = config.effort_profile(effort)
        engine = prof["engine"]
        quality = prof["quality"]
        eff_overrides = prof["overrides"]
    ctl = runctl.RunControl(cancel=cancel, on_stage=on_stage,
                            overrides={**eff_overrides, **(overrides or {})})
    token = runctl.activate(ctl)
    try:
        return _run_check_impl(path, doc_class, use_vision=use_vision,
                               keep_renders=keep_renders, lang=lang,
                               sap_image=sap_image, apply_precedent=apply_precedent,
                               quality=quality, web_evidence=web_evidence,
                               enforce_approvals=enforce_approvals, engine=engine,
                               sap_bp=sap_bp, effort=effort)
    finally:
        runctl.deactivate(token)


def _run_check_impl(path: Path, doc_class: str, use_vision: bool = True,
                    keep_renders: bool = False,
                    lang: str = "en", sap_image: Path | None = None,
                    apply_precedent: bool = True, quality: bool = False,
                    web_evidence: bool | None = None, enforce_approvals: bool = True,
                    engine: str | None = None, sap_bp: str = "",
                    effort: int | None = None) -> CheckResult:
    """apply_precedent=False is for eval: metrics must measure the MACHINE,
    not the operator's stored answers. quality=True forces the strong tier.
    web_evidence: None -> honour the MDMDOC_WEB_EVIDENCE env flag; True/False
    force it (eval passes False — network calls are non-deterministic).
    enforce_approvals=True is the live HARD GATE (only human-approved rules fire);
    eval passes False to measure the raw rules.
    engine: None/'' -> the configured default (config.engine_mode()); otherwise
    one of config.ENGINE_MODES. A mode that needs the LLM auto-degrades to
    'deterministic' (with a WARNING finding) when the model host is down —
    a check NEVER fails because Ollama is unreachable."""
    from . import runctl
    t0 = time.time()
    config.ensure_dirs()
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    # --- analysis engine resolution + LLM health -------------------------------
    engine_req = (engine or "").strip().lower() or config.engine_mode()
    if engine_req not in config.ENGINE_MODES:
        engine_req = "auto"
    engine_eff, llm_ok = engine_req, True
    if engine_req != "deterministic":
        from . import model_client as mc
        try:
            mc.preflight()
        except Exception:
            llm_ok = False
            engine_eff = "deterministic"
    if engine_eff == "deterministic":
        use_vision = False   # the vision reader is an LLM too
    elif engine_eff == "llm-first":
        quality = True       # strong tier from the start

    # run id = content hash (artifacts of a re-run overwrite the same dir)
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    run_id = h.hexdigest()[:16]
    rdir = runstore.render_dir(run_id)

    container_note = ""
    runctl.checkpoint("resolve", 2)
    _sfx = path.suffix.lower()
    _is_wb_container = False
    if _sfx in (".xlsx", ".xlsm"):
        from . import office_embed
        _is_wb_container = office_embed.workbook_has_embeddings(path)
    if _sfx == ".zip" or _sfx in (".eml", ".msg") or _is_wb_container:
        path, container_note = _resolve_container(path, run_id)
    if doc_class in ("auto", ""):
        doc_class = stage_a.sniff_doc_class(path)

    runctl.checkpoint("perception", 8)
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
    runctl.checkpoint("extraction", 40)
    ext = stage_b.extract(raw, quality=quality, policy=policy, engine=engine_eff)
    # evidence ladder (P6): a bounded SECOND perception pass when critical
    # fields are still missing and unread pages show deterministic evidence
    # they can close the gap (the controller loop lives here, not in stage_b)
    from . import ladder
    runctl.checkpoint("ladder", 62)
    ext, ladder_meta = ladder.climb(path, raw, ext, rdir, t0=t0, quality=quality,
                                    policy=policy, engine=engine_eff,
                                    use_vision=use_vision)
    if container_note:
        ext.warnings.insert(0, container_note)

    # rules -> verdict (+ optional SAP comparison as extra findings)
    runctl.checkpoint("rules", 76)
    rules_trace: list = []
    findings = run_rules(ext, lang=lang, policy=policy,
                         enforce_approvals=enforce_approvals, trace=rules_trace)
    from .rules.engine import Finding as _Finding
    if engine_req != "deterministic" and not llm_ok:
        findings.insert(0, _Finding(
            "ENGINE-001", "WARNING", None,
            f"LLM host unreachable — automatic fallback to the deterministic engine "
            f"(OCR + patterns only; narrative fields may be empty). "
            f"Requested engine: {engine_req}."))
    if ext.engine_compare:
        _dis = [r["field"] for r in ext.engine_compare if r["agree"] is False]
        if _dis:
            findings.insert(0, _Finding(
                "ENGINE-002", "WARNING", None,
                f"dual-engine comparison: the deterministic and LLM engines disagree "
                f"on {len(_dis)} field(s): {', '.join(_dis)} — see the engine table"))
        else:
            findings.insert(0, _Finding(
                "ENGINE-003", "NOTE", None,
                f"dual-engine comparison: {len(ext.engine_compare)} ID field(s) "
                f"checked, the two engines agree"))
    sap_rows: list = []
    sap_kind = None
    runctl.checkpoint("sap-compare", 84)
    if sap_image is not None:
        sap_image = sap_image.expanduser().resolve()
        suffix = sap_image.suffix.lower()
        def _sap_internal_error(e: Exception) -> None:
            # Fail closed: the operator explicitly attached SAP data; a compare
            # that crashed must neither abort the whole run (old behavior) nor
            # let an un-compared ACCEPT stand (C10). Distinct from the visible
            # bad-INPUT SapTableError path, which stays warning-only.
            ext.warnings.append(f"SAP comparison failed internally ({e.__class__.__name__}) "
                                "— the document was NOT verified against SAP")
            findings.append(_Finding(
                "SAP-014", "WARNING", "NEED_MANUAL_REVIEW",
                "SAP comparison was requested but aborted with an internal error — "
                "the document was not verified against SAP; review manually."))

        if suffix in (".xlsx", ".xlsm"):
            # SAP TABLE EXPORT (BUT0BK bank details / BUT000 BP general data) —
            # local, deterministic, no vision. BUT0BK↔bank docs, BUT000↔w9 docs.
            from . import sap_compare, sap_tables
            sap_kind = "table"
            try:
                kind, trows = sap_tables.load(sap_image)
            except sap_tables.SapTableError as e:
                ext.warnings.append(f"SAP table export not usable — comparison skipped ({e})")
                kind, trows = None, []
            try:
                if kind == "BUT0BK" and doc_class == "bank":
                    row, sel_findings, _others = sap_tables.select_row(trows, ext, sap_bp)
                    findings += sel_findings
                    if row is not None:
                        sfields = sap_tables.to_sap_fields(row)
                        sap_tables.register_secrets(ext, sfields)
                        sap_findings, sap_rows = sap_compare.compare(ext, sfields, policy=policy)
                        findings += sap_findings
                elif kind == "BUT000" and doc_class == "w9":
                    row = sap_tables.select_bp(trows, sap_bp)
                    if row is not None:
                        sap_findings, sap_rows = sap_tables.compare_bp(ext, row, policy=policy)
                        findings += sap_findings
                    else:
                        ext.warnings.append("SAP BUT000 export: no matching partner "
                                            "(pass the partner number) — comparison skipped")
                elif kind:
                    ext.warnings.append(
                        f"SAP export kind {kind} does not match the {doc_class} document "
                        "— comparison skipped")
            except Exception as e:  # noqa: BLE001 — any compare bug fails closed
                _sap_internal_error(e)
        elif doc_class == "bank":
            # SAP SCREENSHOT (bank only) — vision read
            from . import sap_compare
            sap_kind = "image"
            try:
                sap_fields = sap_compare.read_sap_screen(sap_image, ext.vault)
                if sap_fields:
                    sap_findings, sap_rows = sap_compare.compare(ext, sap_fields, policy=policy)
                    findings += sap_findings
                else:
                    from . import model_client as _mc
                    _verr = getattr(_mc, "LAST_VISION_ERROR", "")
                    ext.warnings.append("SAP screenshot could not be read — comparison skipped"
                                        + (f" (vision: {_verr[:160]})" if _verr else ""))
            except Exception as e:  # noqa: BLE001 — any compare bug fails closed
                _sap_internal_error(e)
        else:
            ext.warnings.append("SAP screenshot comparison applies to bank documents; "
                                "for W-9 attach a BUT000 table export instead")

    # confidence gate: fold the already-recorded read signals into a level, and
    # if the document would otherwise ACCEPT on a LOW-confidence read, abstain to
    # manual review (CONF-001). This ONLY ever escalates ACCEPT->NMR — the verdict
    # still comes from the rules (decide() is a pure precedence fold).
    from . import confidence as _conf
    conf = _conf.assess(ext, raw)
    if conf["level"] == "low" and decide(findings) == "ACCEPT":
        from .rules.engine import Finding as _F
        findings.insert(0, _F("CONF-001", "WARNING", "NEED_MANUAL_REVIEW",
            "low-confidence read — held for manual review: "
            + "; ".join(conf["reasons"][:4])))
    verdict = decide(findings)
    # AFTER sap compare — its values are secrets too. Under the tin-only gate the
    # run artifacts legitimately carry full banking values, so only TIN secrets
    # are enforced there; training-data paths always get the full strict set.
    secrets = ext.vault.tin_secrets() if gate == "tin-only" else ext.vault.secrets()

    # operator precedent: a confirmed label for THIS document (by content hash)
    # overrides the machine verdict/doc_type — feedback must stick immediately.
    # Guardrail (C11): a precedent may TIGHTEN freely, but RELAXING the machine
    # verdict (e.g. rule REJECT -> stored ACCEPT) applies only when the label
    # carries an explicit verdict confirmation — a single mislabel must not
    # silently disarm the rules forever.
    model_verdict, model_doc_type = verdict, ext.doc_type
    precedent = _find_precedent(run_id) if apply_precedent else None
    precedent_relax_blocked = False
    if precedent:
        from .rules.engine import Finding
        from .verdict import RANK
        gold_v = precedent.get("verdict_gold") or verdict
        gold_t = precedent.get("doc_type_gold") or ext.doc_type
        relaxing = RANK.get(gold_v, 99) < RANK.get(verdict, -1)
        note = f" Operator note: {precedent['notes']}" if precedent.get("notes") else ""
        if relaxing and not precedent.get("verdict_confirmed"):
            precedent_relax_blocked = True
            if gold_t != ext.doc_type:   # doc_type memory still applies (not verdict-relaxing)
                ext.doc_type = gold_t
                ext.provenance["doc_type"] = {"source": "precedent", "page": None}
            findings.insert(0, Finding(
                "OPERATOR-2", "WARNING", None,
                f"Stored operator precedent wants to RELAX the machine verdict "
                f"{verdict} → {gold_v} but was saved without explicit verdict "
                f"confirmation — machine verdict kept. Re-review the document "
                f"with 'confirm verdict' checked to apply it.{note}"))
        elif gold_v != verdict or gold_t != ext.doc_type:
            relax_tag = " — RELAXED the machine verdict (explicitly confirmed)" if relaxing else ""
            findings.insert(0, Finding(
                "OPERATOR-1", "WARNING" if relaxing else "NOTE", None,
                f"Operator precedent ({precedent.get('ts', '')}): doc_type={gold_t}, "
                f"verdict={gold_v} — overrides the machine result "
                f"({model_doc_type}/{model_verdict}){relax_tag}.{note}"))
            verdict, ext.doc_type = gold_v, gold_t
            ext.provenance["doc_type"] = {"source": "precedent", "page": None}

    # External evidence (opt-in): corroborate PUBLIC identifiers against outside
    # registries. Runs AFTER the verdict is decided and only ever yields NOTE
    # findings — it is structurally impossible for the web to move the verdict.
    from . import web_enrichment as webenr
    do_web = web_evidence if web_evidence is not None else webenr.enabled()
    web_rows: list = []
    if do_web:
        # force=True: do_web already carries the opt-in decision (env flag or the
        # operator's explicit web_evidence=True request from the console button)
        web_findings, web_rows = webenr.gather(ext, policy=policy, force=True)
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
                                     "notes": precedent.get("notes", ""),
                                     "relax_blocked": precedent_relax_blocked}
    if sap_rows:
        pub["sap_compare"] = sap_rows

    from .estimate import shape_key
    runctl.checkpoint("report", 94)
    report_md = rpt.render_report(pub, findings, verdict, lang=lang)
    meta = {"path": str(path), "file_name": path.name, "doc_class": doc_class,
            "run_id": run_id, "ts": runstore.now_iso(), "model": ext.model_id,
            "use_vision": use_vision, "duration_s": round(time.time() - t0, 1),
            "tier": ext.tier, "escalated_because": ext.escalated_because,
            "engine_requested": engine_req, "engine_effective": engine_eff,
            "llm_ok": llm_ok,
            "has_text_layer": raw.has_text_layer, "quality": quality,
            "signature_pass": bool(raw.signature_probe), "web_evidence": bool(do_web),
            "shape_key": shape_key(doc_class, raw.has_text_layer, use_vision,
                                   sap_image is not None, quality),
            "sap_path": str(sap_image) if sap_image is not None else None,
            "sap_kind": sap_kind,
            "confidence": conf["level"], "confidence_reasons": conf["reasons"],
            "ladder": ladder_meta, "effort": effort}
    report_json = rpt.build_json(pub, findings, verdict, meta)

    # reasoning.md is BUILT MASKED regardless of the operator display policy:
    # its purpose is to be pasted into an EXTERNAL LLM for critique — banking
    # values follow the egress spirit of web_enrichment (masked), TIN always
    from . import reasoning as _reasoning
    from .privacy import scrub_text as _scrub
    reasoning_md = _reasoning.build_reasoning(
        meta, stage_a.to_public(raw, ext.vault, policy="masked"),
        ext.to_public(policy="masked"), findings,
        verdict, conf, ladder_meta, sap_rows, rules_trace,
        container_note=container_note)
    # crosscheck/warning lines were rendered under the OPERATOR policy at
    # extraction time — strict-scrub the assembled export end-to-end
    reasoning_md = _scrub(reasoning_md, ext.vault, policy="strict")

    runstore.write(run_id, "meta.json", meta, secrets, policy=gate)
    runstore.write(run_id, "reasoning.md", reasoning_md, secrets, policy=gate)
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


def _extract_eml_attachments(eml: Path, out: Path) -> None:
    """Save real document attachments (pdf / sizeable images) of an email into
    `out`. Inline logos and signature images are skipped by size."""
    import email
    from email import policy
    try:
        msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    except Exception:
        return
    for part in msg.walk():
        fn = part.get_filename()
        if not fn:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload or len(payload) < 10_000:
            continue
        ext = Path(fn).suffix.lower()
        if ext == ".pdf" or ext in config.IMAGE_EXTS:
            out.mkdir(parents=True, exist_ok=True)
            (out / Path(fn).name.replace("/", "_")).write_bytes(payload)


def _resolve_container(cpath: Path, run_id: str) -> tuple[Path, str]:
    """.zip / .eml packets: extract into a persistent inbox folder and analyse
    the single best DOCUMENT inside — a bank confirmation letter outranks
    declarations, invoices, email bodies and logos; an emailed invoice still
    outranks the bare email body (the invoice IS the evidence to judge). The
    report states what was chosen and what was skipped. Full multi-document
    batch mode is on the roadmap."""
    from . import fields as fdefs, ocr, office_embed
    out = config.INBOX_DIR / f"{run_id}__packet"
    ext0 = cpath.suffix.lower()
    if ext0 == ".zip":
        import zipfile
        out.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(cpath) as z:
            z.extractall(out)  # ZipFile.extract sanitizes absolute/.. member names
    elif ext0 in (".xlsx", ".xlsm"):
        # SAP request workbook carrying its support documents as embedded OLE
        # objects (xl/embeddings) — the documents are the evidence, not the form
        saved = office_embed.extract_workbook_embeddings(cpath, out)
        if not saved:
            raise UnreadableDocument(
                "workbook contains no embedded documents — attach the actual "
                "bank/W-9 document, or upload this workbook in the Template "
                "slot to compare its values against a document")
    elif ext0 == ".msg":
        # real MAPI parsing (the stdlib email module cannot read OLE .msg —
        # this path silently yielded nothing before D7)
        office_embed.extract_msg_attachments(cpath, out)
        if not out.exists() or not any(out.iterdir()):
            return cpath, ""  # plain message without document attachments
    else:  # .eml — analyse the attached documents, not the mail body
        _extract_eml_attachments(cpath, out)
        if not out.exists():
            return cpath, ""  # plain email without document attachments
    # emails inside a zip carry their documents as attachments — expand them too
    for eml in list(out.rglob("*.eml")):
        _extract_eml_attachments(eml, out / (eml.stem[:40] + "_attachments"))
    for msg in list(out.rglob("*.msg")):
        office_embed.extract_msg_attachments(msg, out / (msg.stem[:40] + "_attachments"))
    for wb in list(out.rglob("*.xlsx")) + list(out.rglob("*.xlsm")):
        if office_embed.workbook_has_embeddings(wb):
            office_embed.extract_workbook_embeddings(wb, out / (wb.stem[:40] + "_embedded"))

    docs = []
    for p in sorted(out.rglob("*")):
        if not p.is_file() or p.name.startswith("._"):
            continue
        e = p.suffix.lower()
        if e == ".pdf" or e in config.EMAIL_EXTS \
                or (e in config.IMAGE_EXTS and p.stat().st_size > 50_000):
            docs.append(p)
    if not docs:
        if ext0 in config.EMAIL_EXTS:
            return cpath, ""
        raise UnreadableDocument(
            "container holds no readable document (pdf/image/eml) — extract it manually")

    def _peek_text(p: Path) -> str:
        """Text layer, else ONE fast OCR of page 1 — the packet choice needs eyes."""
        if p.suffix.lower() == ".pdf":
            try:
                import fitz
                d = fitz.open(p)
                t = "".join(d[i].get_text() for i in range(min(d.page_count, 3)))
                d.close()
                if len(t.strip()) >= 40:
                    return t
            except Exception:
                pass
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                pngs = (ocr.render_pdf_pages(p, Path(td), max_pages=1)
                        if p.suffix.lower() == ".pdf" else [ocr.prepare_image(p, Path(td))])
                return ocr.tesseract_text(pngs[0]) if pngs else ""
        except Exception:
            return ""

    def _score(p: Path) -> int:
        e = p.suffix.lower()
        if e in config.EMAIL_EXTS:
            return 1
        s = 4 if e == ".pdf" else 2
        t = _peek_text(p)
        s += fdefs.page_score(t, "bank")
        m = fdefs.page_markers(t)
        if m["bank_letter"]:
            s += 10
        if m["invoice"] and not m["bank_letter"]:
            s -= 3
        return s

    docs.sort(key=_score, reverse=True)
    chosen, rest = docs[0], [d.name for d in docs[1:]]
    note = (f"packet ({cpath.name}): analysed '{chosen.name}'"
            + (f"; other documents inside NOT analysed: {', '.join(rest)}" if rest else ""))
    return chosen, note


def _find_precedent(run_id: str) -> dict | None:
    """The confirmed label for this exact document (content hash), if any."""
    from .dataset import load_labels
    for lab in load_labels():
        if lab.get("doc_sha256") == run_id and lab.get("confirmed"):
            return lab
    return None
