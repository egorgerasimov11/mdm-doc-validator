#!/usr/bin/env python3
"""mdmdoc — Local MDM Document Validator CLI.

Commands: check (auto), check-bank, check-w9, review, eval, train, export-lora, runs, doctor.
Exit codes: 0 ACCEPT, 1 REJECT, 2 WARNING/NEED_MANUAL_REVIEW, 3 Ollama down, 4 unreadable.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config, model_client as mc


def _clean_path(parts) -> str:
    """Turn CLI token(s) into a usable path. Handles unquoted paths with spaces
    (parts join back), Finder drag-and-drop (backslash-escaped spaces, trailing
    space), file:// URLs, and surrounding quotes."""
    raw = " ".join(parts) if isinstance(parts, (list, tuple)) else str(parts)
    raw = raw.strip().strip('"').strip("'").strip()
    if raw.startswith("file://"):
        raw = unquote(urlparse(raw).path)
    raw = raw.replace("\\ ", " ").replace("\\~", "~")
    return raw


def _cmd_check(args, doc_class: str) -> int:
    from .pipeline import UnreadableDocument, run_check
    from .verdict import exit_code
    engine = (getattr(args, "engine", "") or "").strip().lower()
    if engine != "deterministic":   # deterministic runs need no model host at all
        try:
            mc.preflight()
        except mc.OllamaUnavailable as e:
            print(str(e), file=sys.stderr)
            print("hint: `--engine deterministic` analyses without the LLM "
                  "(OCR + patterns + rules only).", file=sys.stderr)
            return config.EXIT_OLLAMA_DOWN
    path = _clean_path(args.path)
    if not Path(path).expanduser().exists():
        print(f"file not found: {path}\n"
              "hint: wrap the path in quotes, or just drag the file from Finder into "
              "the terminal after typing `mdmdoc check-bank ` (with a trailing space).",
              file=sys.stderr)
        return config.EXIT_UNREADABLE
    sap_image = None
    if getattr(args, "sap", None):
        sap_image = Path(_clean_path(args.sap)).expanduser()
        if not sap_image.exists():
            print(f"SAP data not found: {sap_image}", file=sys.stderr)
            return config.EXIT_UNREADABLE
    from .estimate import estimate_seconds, human, sniff_text_layer
    est = estimate_seconds(doc_class, sniff_text_layer(Path(path)),
                           use_vision=not args.no_vision,
                           sap=sap_image is not None, quality=args.quality)
    print(f"expected duration: {human(est)}", file=sys.stderr)
    web_evidence = True if getattr(args, "web_evidence", False) else None
    try:
        res = run_check(Path(path), doc_class, use_vision=not args.no_vision,
                        keep_renders=args.keep_renders, lang=args.lang,
                        sap_image=sap_image, quality=args.quality,
                        web_evidence=web_evidence, engine=engine or None,
                        sap_bp=getattr(args, "sap_bp", "") or "")
    except FileNotFoundError as e:
        print(f"file not found: {e}", file=sys.stderr)
        return config.EXIT_UNREADABLE
    except UnreadableDocument as e:
        print(f"UNREADABLE: {e}", file=sys.stderr)
        return config.EXIT_UNREADABLE
    if args.json:
        print(res.report_json)
    else:
        print(res.report_md)
        print(f"(run {res.run_id} — artifacts in runs/{res.run_id}/; "
              f"correct me with: mdmdoc review {res.run_id})")
    if args.report:
        Path(args.report).write_text(res.report_md, encoding="utf-8")
    return exit_code(res.verdict)


def _cmd_review(args) -> int:
    from .review import review_run
    return review_run(args.run, open_doc=args.open)


def _cmd_eval(args) -> int:
    if getattr(args, "rescore", False):
        # re-scores stored predictions — no model host needed at all
        from .evalrun import run_rescore
        return run_rescore(tag=args.tag, record=bool(args.tag))
    engine = getattr(args, "engine", "") or None
    if engine != "deterministic":
        # a deterministic eval runs fully offline (synthetic corpus in CI)
        try:
            mc.preflight()
        except mc.OllamaUnavailable as e:
            print(str(e), file=sys.stderr)
            return config.EXIT_OLLAMA_DOWN
    from .evalrun import run_eval
    return run_eval(only=args.only, limit=args.limit, tag=args.tag,
                    scenario=args.scenario, dataset=getattr(args, "dataset", "real"),
                    engine=engine)


def _cmd_rules_stats(args) -> int:
    from . import rule_stats
    payload = rule_stats.build(
        runs_dir=Path(args.runs) if args.runs else None,
        labels_path=Path(args.labels) if args.labels else None)
    stats = payload["per_rule"]
    fired_any = [s for s in stats if s["fired"]]
    if not fired_any:
        print(f"no runs found at {args.runs or config.RUNS_DIR} — nothing to aggregate "
              "(run this on the host that holds the run history, e.g. the mini)")
    else:
        print("| rule | tier | effect | fired | confirmed | precision | wilson_lb | age_d |")
        print("|---|---|---|---|---|---|---|---|")
        for s in stats:
            if not s["fired"]:
                continue
            print(f"| {s['doc_class']}:{s['rule_id']} | {s['tier'] or '—'} "
                  f"| {s['verdict_effect'] or 'NOTE'} | {s['fired']} "
                  f"| {s['fired_confirmed']} | {s['precision']} | {s['wilson_lb']} "
                  f"| {s['age_days']} |")
    if payload["proposals"]:
        print("\nPROPOSALS (approve in the panel — never auto-applied):")
        for p in payload["proposals"]:
            print(f"  {p['kind']}: {p['doc_class']}:{p['rule_id']} "
                  f"{p['from_tier']} -> {p['to_tier']}  evidence={p['evidence']}")
    else:
        print("\nno tier proposals (thresholds not met)")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    if args.write:
        from .rule_stats import write_report
        print(f"written: {write_report(payload)}")
    return 0


def _cmd_synth_gen(args) -> int:
    from . import synth
    if args.check:
        return synth.check(seed=args.seed)
    res = synth.generate(seed=args.seed)
    print(f"generated {res['count']} synthetic docs "
          f"(truth vs deterministic agreement {res['agreement']}) -> {res['labels_path']}")
    return 0


def _cmd_train(args) -> int:
    from .fewshot import build_fewshot
    from .modelfile import build_modelfile
    rc = 0
    did = False
    if args.fewshot:
        did = True
        rc |= build_fewshot(k=args.k)
    if args.modelfile:
        did = True
        rc |= build_modelfile(apply=args.apply)
    if not did:
        print("nothing to do: pass --fewshot and/or --modelfile", file=sys.stderr)
        return 1
    return rc


def _cmd_export_lora(args) -> int:
    from .lora_export import export_lora
    return export_lora(min_labels=args.min_labels, force=args.force, split=args.split)


def _cmd_serve(args) -> int:
    import os
    if args.api_only:
        os.environ["MDMDOC_MODE"] = "api-only"
    import uvicorn
    uvicorn.run("mdmdoc.server.app:create_app", factory=True,
                host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_ui(args) -> int:
    import threading
    import time
    import webbrowser

    import requests
    import uvicorn

    port = args.port
    url = f"http://127.0.0.1:{port}"

    # a server is already running (e.g. the LaunchAgent) -> just open the console
    try:
        if requests.get(f"{url}/health", timeout=1).ok:
            print(f"mdmdoc console already running: {url}/ui")
            webbrowser.open(f"{url}/ui")
            return 0
    except Exception:
        pass

    def open_when_up() -> None:
        for _ in range(30):
            try:
                if requests.get(f"{url}/health", timeout=1).ok:
                    webbrowser.open(f"{url}/ui")
                    return
            except Exception:
                pass
            time.sleep(0.5)

    threading.Thread(target=open_when_up, daemon=True).start()
    print(f"mdmdoc console: {url}/ui  (Ctrl-C to stop)")
    uvicorn.run("mdmdoc.server.app:create_app", factory=True,
                host="127.0.0.1", port=port, log_level="info")
    return 0


def _cmd_runs(args) -> int:
    from .runstore import list_runs
    rows = list_runs()
    if not rows:
        print("no runs yet")
        return 0
    for r in rows:
        print(f"{r['run_id']}  {r['doc_class']:4}  {r['doc_type']:18}  "
              f"{r['verdict']:19}  {r['file']}")
    return 0


def _cmd_doctor(args) -> int:
    print(f"project root : {config.PROJECT_ROOT}")
    try:
        models = mc.preflight()
        print(f"ollama       : OK at {mc.host()} — {mc.host_source()} ({len(models)} models)")
        for role in mc.ROLES:
            m = mc.resolve(role)
            mark = "OK" if (m in models or f"{m}:latest" in models) else "MISSING (will fall back)"
            print(f"  {role:7} -> {m}  [{mark}]")
    except mc.OllamaUnavailable as e:
        print(f"ollama       : DOWN — {e}")
        return config.EXIT_OLLAMA_DOWN
    tess = shutil.which("tesseract")
    if tess:
        try:
            langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                                   timeout=10).stdout.decode().split()[1:]
            print(f"tesseract    : {tess} (langs: {', '.join(sorted(langs))})")
        except Exception:
            print(f"tesseract    : {tess}")
    else:
        print("tesseract    : MISSING (brew install tesseract tesseract-lang) — "
              "scanned docs will rely on the vision model only")
    for name, p in (("rules", config.RULES_DIR), ("prompts", config.PROMPTS_DIR),
                    ("templates", config.TEMPLATES_DIR)):
        print(f"{name:13}: {'OK' if p.exists() else 'MISSING'} ({p})")
    from .dataset import count_labels
    print(f"labels       : {count_labels()} in {config.LABELS_PATH}")
    from . import web_enrichment as webenr
    we = "ON (opt-in)" if webenr.enabled() else "off (MDMDOC_WEB_EVIDENCE=1 to enable)"
    print(f"web evidence : {we} — advisory external registry hints, never decides verdicts")
    return 0


def _cmd_skill_rules(args) -> int:
    """Show an mdm-*-checker skill's active rules and how they map to the validator."""
    from . import skill_rules as sr
    try:
        path = sr.resolve_skill(args.skill)
    except FileNotFoundError as e:
        print(e)
        return 1
    rules = sr.active_rules(path)
    print(f"{len(rules)} active rule(s) in {args.skill}  ({path})\n")
    covered, advisory, review = [], [], []
    for r in rules:
        cov = r["coverage"]
        bucket = review if not cov else (advisory if cov.startswith(("advisory", "needs")) else covered)
        bucket.append(r)
        print(f"{r['id']}  [{r['severity'] or '?':8}] {r['header']}")
        print(f"      -> {cov or 'REVIEW — no mapping yet; decide: promote to a rule or mark advisory'}")
    print(f"\nmechanized: {len(covered)}  ·  advisory/needs-context: {len(advisory)}  ·  to review: {len(review)}")
    if review:
        print("Run the mdmdoc-skill-sync procedure to promote reviewable rules into rules/*.yaml.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mdmdoc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    _check_help = {"auto": "validate any supported document (bank vs W-9/W-8 auto-detected)",
                   "bank": "validate a banking document",
                   "w9": "validate a W-9/W-8 form"}
    for name, doc_class in (("check", "auto"), ("check-bank", "bank"), ("check-w9", "w9")):
        p = sub.add_parser(name, help=_check_help[doc_class])
        p.add_argument("path", nargs="+", help="path to the PDF/image (quotes optional — "
                       "spaces and Finder drag-and-drop are handled)")
        p.add_argument("--json", action="store_true", help="print machine JSON instead of the report")
        p.add_argument("--report", help="also write the report to this path")
        p.add_argument("--lang", choices=("en", "ru"), default="en")
        p.add_argument("--no-vision", action="store_true", help="skip the vision model (tesseract only)")
        p.add_argument("--keep-renders", action="store_true",
                       help="keep page renders (SENSITIVE: pixels contain full account data)")
        p.add_argument("--quality", action="store_true",
                       help="force the strong model tier (slower, thorough)")
        p.add_argument("--engine", choices=config.ENGINE_MODES, default="",
                       help="analysis engine: auto (default), deterministic (no LLM), "
                            "llm-first (strong tier), dual (compare both engines)")
        p.add_argument("--web-evidence", action="store_true",
                       help="corroborate PUBLIC ids (routing/SWIFT/bank & company names) "
                            "against external registries — advisory NOTE hints, never a "
                            "verdict; also enabled by MDMDOC_WEB_EVIDENCE=1")
        # SAP comparison: a Bank Details SCREENSHOT (bank docs) OR a table export
        # .xlsx (BUT0BK bank details / BUT000 BP general data — works for w9 too).
        p.add_argument("--sap", help="SAP data to compare against: a Bank Details "
                                     "screenshot (.png/.jpg) or a BUT0BK/BUT000 "
                                     "table export (.xlsx)")
        p.add_argument("--sap-bp", default="", help="SAP Business Partner number — "
                                                    "selects the row in a table export "
                                                    "(else reverse-lookup by the document)")
        p.set_defaults(func=lambda a, dc=doc_class: _cmd_check(a, dc))

    p = sub.add_parser("review", help="interactively correct a run -> labeled example")
    p.add_argument("run", help="run id, prefix, source path, or 'last'")
    p.add_argument("--open", action="store_true", help="open the original document")
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser("eval", help="re-run the pipeline over all labels and score it")
    p.add_argument("--only", choices=("bank", "w9"))
    p.add_argument("--limit", type=int)
    p.add_argument("--tag", default="")
    p.add_argument("--scenario", default=None,
                   help="only labels carrying this scenario tag (e.g. w9_boxed_tin)")
    p.add_argument("--dataset", choices=("real", "synthetic", "both"), default="real",
                   help="which corpus: real (headline 18-doc stream), synthetic "
                        "(PII-free stratum, separate synthetic_* artifacts), both")
    p.add_argument("--engine", choices=config.ENGINE_MODES, default="",
                   help="force the analysis engine; 'deterministic' runs fully "
                        "offline (no Ollama preflight)")
    p.add_argument("--rescore", action="store_true",
                   help="re-score the LAST eval's stored predictions under the current "
                        "scorers (strict fidelity + lenient column) — no model calls; "
                        "--tag records the anchor in history")
    p.set_defaults(func=_cmd_eval)

    p = sub.add_parser("synth-gen",
                       help="(re)generate the PII-free synthetic eval corpus "
                            "(eval/synthetic/) with known ground truth")
    p.add_argument("--seed", type=int, default=20260709)
    p.add_argument("--check", action="store_true",
                   help="staleness self-check: regenerate to a temp dir and "
                        "compare labels + per-doc text against the committed corpus")
    p.set_defaults(func=_cmd_synth_gen)

    p = sub.add_parser("rules-stats",
                       help="per-rule firing stats + tier promotion PROPOSALS "
                            "(П7 governance; approval happens in the panel)")
    p.add_argument("--runs", default="", help="runs dir (default: this host's runs/)")
    p.add_argument("--labels", default="", help="labels.jsonl (default: this host's)")
    p.add_argument("--json", action="store_true", help="print the full JSON payload")
    p.add_argument("--write", action="store_true",
                   help="persist eval/rule_stats.json for the panel")
    p.set_defaults(func=_cmd_rules_stats)

    p = sub.add_parser("train", help="build few-shot prompts and/or a custom Ollama model from labels")
    p.add_argument("--fewshot", action="store_true")
    p.add_argument("--modelfile", action="store_true")
    p.add_argument("--apply", action="store_true", help="actually run `ollama create` (server must already be up)")
    p.add_argument("--k", type=int, default=2, help="few-shot examples per doc class")
    p.set_defaults(func=_cmd_train)

    p = sub.add_parser("export-lora", help="export labels as an MLX LoRA dataset (see TRAINING.md)")
    p.add_argument("--min-labels", type=int, default=100)
    p.add_argument("--force", action="store_true")
    p.add_argument("--split", type=float, default=0.85)
    p.set_defaults(func=_cmd_export_lora)

    p = sub.add_parser("ui", help="start the operator console and open it in the browser")
    p.add_argument("--port", type=int, default=config.SERVER_DEFAULT_PORT)
    p.set_defaults(func=_cmd_ui)

    p = sub.add_parser("serve", help="run the HTTP server headless (Docker/BTP)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=config.SERVER_DEFAULT_PORT)
    p.add_argument("--api-only", action="store_true",
                   help="core API only: no UI, no teach/training routes")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("runs", help="list past runs")
    p.set_defaults(func=_cmd_runs)

    p = sub.add_parser("doctor", help="check ollama/tesseract/models/dirs")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("skill-rules", help="show an mdm-*-checker skill's rules + validator mapping")
    p.add_argument("skill", help="skill name (mdm-w9-checker), skill dir, or dynamic_rules.md path")
    p.set_defaults(func=_cmd_skill_rules)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
