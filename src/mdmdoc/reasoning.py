#!/usr/bin/env python3
"""
reasoning.py — assemble the FULL decision trace of one run into a single
exportable markdown artifact (D6): how the pipeline perceived the document,
what it extracted through which tier, every guard that spoke, how the
signature/ladder/confidence folded, what EVERY rule did (fired / did not fire /
not applicable / held by the approval gate) and how the verdict fell out.

Purpose: the operator downloads reasoning.md and pastes it into an external
LLM (GPT etc.) for critique — so it must be self-contained and честный.

Built ONLY from already-policy-filtered public structures (ext.to_public /
stage_a.to_public / masked compare rows), so masking is inherited; the write
still passes the leak gate in runstore.write as defense in depth. Zero model
calls — pure assembly of what the run already recorded.
"""
from __future__ import annotations


def _kv(label: str, value) -> str:
    v = value if value not in (None, "", []) else "—"
    return f"- **{label}:** {v}"


def _field_str(v) -> str:
    if isinstance(v, dict):
        return str(v.get("masked") or ("present" if v.get("present") else "—"))
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v).strip() or "—"


def _compare_table(rows: list) -> list[str]:
    out = ["", "| field | document | other side | result |", "|---|---|---|---|"]
    for r in rows:
        note = f" ({r.get('note')})" if r.get("note") else ""
        out.append(f"| {r.get('field')} | {r.get('doc') or '—'} | "
                   f"{r.get('sap') or '—'} | {r.get('status')}{note} |")
    return out


def build_reasoning(meta: dict, stage_a_pub: dict, pub: dict, findings: list,
                    verdict: str, conf: dict, ladder_meta: dict | None,
                    sap_rows: list | None, rules_trace: list | None,
                    container_note: str = "", tpl_rows: list | None = None) -> str:
    L: list[str] = []
    f = pub.get("fields", {}) or {}

    L.append(f"# Decision trace — {meta.get('file_name', '?')}")
    L.append("")
    L.append("Machine-generated, self-contained reasoning export of one document "
             "check. Sensitive values are masked by policy. Suitable for external "
             "review: every claim below is what the pipeline actually recorded.")
    L.append("")
    L.append("## 1. Run")
    L.append(_kv("run id", meta.get("run_id")))
    L.append(_kv("timestamp", meta.get("ts")))
    L.append(_kv("doc_class / doc_type", f"{pub.get('doc_class')} / {pub.get('doc_type')}"))
    L.append(_kv("engine", f"{meta.get('engine_effective')} (requested "
                           f"{meta.get('engine_requested')}, LLM ok: {meta.get('llm_ok')})"))
    L.append(_kv("effort", meta.get("effort") or "legacy (no slider value)"))
    L.append(_kv("tier", meta.get("tier")))
    L.append(_kv("models", meta.get("model")))
    L.append(_kv("duration", f"{meta.get('duration_s')}s"))

    if container_note:
        L.append("")
        L.append("## 2. Container resolution")
        L.append(container_note)

    L.append("")
    L.append("## 3. Perception (Stage A)")
    L.append(_kv("pages total / read", f"{stage_a_pub.get('pages')} / "
                                       f"{stage_a_pub.get('pages_used')}"))
    L.append(_kv("text layer", stage_a_pub.get("has_text_layer")))
    if stage_a_pub.get("text_layer_garbage"):
        L.append("- **text layer was GARBAGE** (unreadable font encoding) — the "
                 "document was rerouted to the scan/OCR branch")
    for key, label in (("bank_letter_pages", "bank-letter pages"),
                       ("invoice_pages", "invoice pages"),
                       ("w9_pages", "W-9/W-8 form pages"),
                       ("w8_cert_page", "W-8 certification page")):
        v = stage_a_pub.get(key)
        if v not in (None, [], ""):
            L.append(_kv(label, v))
    L.append(_kv("deterministic OCR candidates (masked)",
                 stage_a_pub.get("regex_candidates_masked")))

    L.append("")
    L.append("## 4. Extraction (Stage B)")
    L.append(_kv("fast-tier JSON valid first try", pub.get("json_valid_first_try")))
    esc = pub.get("escalated_because") or meta.get("escalated_because") or []
    if esc:
        L.append(_kv("escalated to the strong tier because", ", ".join(esc)))
    L.append("")
    L.append("Extracted fields (masked view):")
    for k, v in f.items():
        L.append(f"- {k}: {_field_str(v)}")
    warnings = pub.get("warnings") or []
    if warnings:
        L.append("")
        L.append("Guard/pipeline warnings (each line is a deterministic guard or "
                 "pipeline note that changed or flagged something):")
        for w in warnings:
            L.append(f"- {w}")
    cross = pub.get("crosscheck") or []
    if cross:
        L.append("")
        L.append("OCR cross-check notes:")
        for c in cross:
            L.append(f"- {c}")

    sig = pub.get("signature_probe") or {}
    if sig:
        L.append("")
        L.append("## 5. Signature")
        L.append(_kv("signed / kind", f"{_field_str(f.get('signed'))} / "
                                      f"{f.get('signature_kind') or '—'}"))
        L.append(_kv("votes", sig.get("votes")))
        L.append(_kv("e-signature short-circuit", sig.get("esign_short_circuit")))
        L.append(_kv("contested / uncertain / escalated",
                     f"{sig.get('contested')} / {sig.get('uncertain')} / "
                     f"{sig.get('escalated')}"))
        for t in sig.get("trail") or []:
            L.append(f"  - probe {t.get('probe')}: {t.get('vote')} — {t.get('evidence')}")

    if ladder_meta:
        L.append("")
        L.append("## 6. Evidence ladder")
        if ladder_meta.get("used"):
            L.append(_kv("deep-read extra pages", ladder_meta.get("pages")))
            L.append(_kv("to close gaps", ladder_meta.get("gaps")))
            L.append(_kv("extra vision calls", ladder_meta.get("vision_calls")))
        else:
            L.append("- not used" + (f" ({ladder_meta.get('skipped')})"
                                     if ladder_meta.get("skipped") else
                                     " (no critical gap or no promising unread page)"))

    L.append("")
    L.append("## 7. Confidence")
    L.append(_kv("level", conf.get("level")))
    for r in conf.get("reasons") or []:
        L.append(f"- {r}")

    L.append("")
    L.append("## 8. Rules")
    if rules_trace:
        L.append("Every rule the engine evaluated for this document:")
        L.append("")
        L.append("| rule | name | outcome | detail |")
        L.append("|---|---|---|---|")
        for t in rules_trace:
            L.append(f"| {t['rule_id']} | {t.get('name', '')} | {t['outcome']} | "
                     f"{t.get('detail', '')} |")
    L.append("")
    L.append("Findings (what actually fired, incl. gate/engine/pipeline findings):")
    for fd in findings:
        d = fd.to_dict() if hasattr(fd, "to_dict") else dict(fd)
        eff = f" -> {d.get('verdict_effect')}" if d.get("verdict_effect") else ""
        L.append(f"- [{d.get('severity')}]{eff} {d.get('rule_id')}: {d.get('message')}")

    if sap_rows:
        L.append("")
        L.append("## 9. SAP comparison")
        L += _compare_table(sap_rows)

    if tpl_rows:
        L.append("")
        L.append("## 9b. Template (request form) comparison")
        L += _compare_table(tpl_rows)

    prec = pub.get("operator_precedent")
    if prec:
        L.append("")
        L.append("## 10. Operator precedent")
        L.append(_kv("stored verdict applied", prec.get("verdict")))
        L.append(_kv("machine verdict before precedent", prec.get("model_verdict")))
        if prec.get("relax_blocked"):
            L.append("- a RELAXING precedent was blocked (saved without explicit "
                     "verdict confirmation)")

    L.append("")
    L.append("## 11. Verdict")
    L.append(f"**{verdict}** — precedence fold over the findings above "
             "(REJECT > NEED_MANUAL_REVIEW > WARNING > ACCEPT); the verdict comes "
             "ONLY from human-approved rules, never from the model directly.")
    L.append("")
    return "\n".join(L)
