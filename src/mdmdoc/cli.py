#!/usr/bin/env python3
"""mdmdoc — Local MDM Document Validator CLI.

Commands: check-bank, check-w9, review, eval, train, export-lora, runs, doctor.
Exit codes: 0 ACCEPT, 1 REJECT, 2 WARNING/NEED_MANUAL_REVIEW, 3 Ollama down, 4 unreadable.
"""
from __future__ import annotations

import argparse
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
    try:
        mc.preflight()
    except mc.OllamaUnavailable as e:
        print(str(e), file=sys.stderr)
        return config.EXIT_OLLAMA_DOWN
    path = _clean_path(args.path)
    if not Path(path).expanduser().exists():
        print(f"file not found: {path}\n"
              "hint: wrap the path in quotes, or just drag the file from Finder into "
              "the terminal after typing `mdmdoc check-bank ` (with a trailing space).",
              file=sys.stderr)
        return config.EXIT_UNREADABLE
    try:
        res = run_check(Path(path), doc_class, use_vision=not args.no_vision,
                        keep_renders=args.keep_renders, lang=args.lang)
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
    try:
        mc.preflight()
    except mc.OllamaUnavailable as e:
        print(str(e), file=sys.stderr)
        return config.EXIT_OLLAMA_DOWN
    from .evalrun import run_eval
    return run_eval(only=args.only, limit=args.limit, tag=args.tag)


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
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mdmdoc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, doc_class in (("check-bank", "bank"), ("check-w9", "w9")):
        p = sub.add_parser(name, help=f"validate a {'banking document' if doc_class == 'bank' else 'W-9/W-8 form'}")
        p.add_argument("path", nargs="+", help="path to the PDF/image (quotes optional — "
                       "spaces and Finder drag-and-drop are handled)")
        p.add_argument("--json", action="store_true", help="print machine JSON instead of the report")
        p.add_argument("--report", help="also write the report to this path")
        p.add_argument("--lang", choices=("en", "ru"), default="en")
        p.add_argument("--no-vision", action="store_true", help="skip the vision model (tesseract only)")
        p.add_argument("--keep-renders", action="store_true",
                       help="keep page renders (SENSITIVE: pixels contain full account data)")
        p.set_defaults(func=lambda a, dc=doc_class: _cmd_check(a, dc))

    p = sub.add_parser("review", help="interactively correct a run -> labeled example")
    p.add_argument("run", help="run id, prefix, source path, or 'last'")
    p.add_argument("--open", action="store_true", help="open the original document")
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser("eval", help="re-run the pipeline over all labels and score it")
    p.add_argument("--only", choices=("bank", "w9"))
    p.add_argument("--limit", type=int)
    p.add_argument("--tag", default="")
    p.set_defaults(func=_cmd_eval)

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

    p = sub.add_parser("runs", help="list past runs")
    p.set_defaults(func=_cmd_runs)

    p = sub.add_parser("doctor", help="check ollama/tesseract/models/dirs")
    p.set_defaults(func=_cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
