"""`mdmdoc bench …` — argparse for the transcription benchmark.

Every handler imports its module lazily so that `mdmdoc check-*` never pays
for (or depends on) the optional `bench` dependency group.
"""
from __future__ import annotations

import argparse
import sys


def _p(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── manifest ────────────────────────────────────────────────────────────────

def _cmd_manifest_add(a) -> int:
    from . import manifest
    tags = [t for t in (a.tags or "").split(",") if t]
    langs = [t for t in (a.langs or "").split(",") if t]
    pages = [int(x) for x in a.pages.split(",")] if a.pages else None
    added = manifest.add(a.paths, kind=a.kind, langs=langs or None, tags=tags,
                         expected_doc_type=a.type or "", pages=pages, stratum=a.stratum,
                         notes=a.notes or "")
    for d in added:
        _p(f"  + {d.doc_id}  {d.kind:7s} p={len(d.pages)}/{d.pages_total:<3d} "
           f"{','.join(d.langs):10s} {','.join(d.tags)}  {d.path}")
    _p(f"{len(added)} document(s) in manifest now: {len(manifest.load('all'))}")
    return 0


def _cmd_manifest_show(a) -> int:
    from . import manifest
    docs = manifest.load(a.filter)
    for d in docs:
        print(f"{d.doc_id}  {d.kind:7s} {d.stratum:9s} p={len(d.pages)}/{d.pages_total:<3d} "
              f"{','.join(d.langs):10s} [{','.join(d.tags)}]  {d.path}")
    _p(f"{len(docs)} document(s), {sum(len(d.pages) for d in docs)} page(s)")
    return 0


def _cmd_manifest_synth(a) -> int:
    from . import manifest
    n = manifest.build_synthetic()
    _p(f"synthetic stratum: {n} document(s)")
    return 0


def _cmd_manifest_materialize(a) -> int:
    from . import manifest
    n = manifest.materialize()
    _p(f"manifest now points at bench/corpus/ copies ({n} path(s) rewritten)")
    return 0


def _cmd_manifest_sniff(a) -> int:
    import json
    from pathlib import Path
    from . import manifest
    for p in a.paths:
        print(json.dumps(manifest.sniff(Path(p)), ensure_ascii=False, indent=2))
    return 0


# ── render ──────────────────────────────────────────────────────────────────

def _cmd_render(a) -> int:
    from . import manifest
    from ..extract import render
    docs = manifest.load(a.filter)
    spec = render.preset(a.preset)
    n = 0
    for d in docs:
        for idx in d.pages:
            png = render.render_page(d.abs_path, d.render_dir, idx, spec)
            n += 1
            if a.verbose:
                _p(f"  {d.doc_id} p{idx} -> {png}")
    _p(f"rendered {n} page(s) with preset {a.preset}")
    return 0


# ── gold ────────────────────────────────────────────────────────────────────

def _cmd_gold(a) -> int:
    from . import gold
    return gold.cli_gold(a)


def _cmd_gold_review(a) -> int:
    from . import gold
    return gold.cli_review(a)


def _cmd_gold_accept(a) -> int:
    from . import gold
    return gold.cli_accept(a)


def _cmd_gold_fix(a) -> int:
    from . import gold
    return gold.cli_fix(a)


# ── run / report ────────────────────────────────────────────────────────────

def _cmd_run(a) -> int:
    from . import run
    return run.cli_run(a)


def _cmd_report(a) -> int:
    from . import report
    return report.cli_report(a)


def _cmd_worst(a) -> int:
    from . import report
    return report.cli_worst(a)


def _cmd_doctype(a) -> int:
    from . import doctype
    return doctype.cli_doctype(a)


def _cmd_public(a) -> int:
    from . import public
    return public.cli_public(a)


def _cmd_tag_hw(a) -> int:
    from . import public
    n = public.tag_handwriting_from_gold()
    _p(f"tagged handwriting on {n} document(s) from gold")
    return 0


def _cmd_models(a) -> int:
    from . import models
    return models.cli_models(a)


def _cmd_doctor(a) -> int:
    from . import models
    return models.cli_doctor(a)


def _cmd_status(a) -> int:
    from . import models
    return models.cli_status(a)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mdmdoc bench",
                                 description="document-transcription benchmark")
    sub = ap.add_subparsers(dest="bcmd", required=True)

    m = sub.add_parser("manifest", help="corpus manifest (bench/corpus.jsonl)")
    ms = m.add_subparsers(dest="mcmd", required=True)
    p = ms.add_parser("add", help="add documents (containers are expanded)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--kind", choices=("digital", "scan", "photo", "mixed"))
    p.add_argument("--langs", help="comma list, e.g. ko,en (overrides the sniffed guess)")
    p.add_argument("--tags", help="comma list, e.g. core,handwriting,seal")
    p.add_argument("--type", help="expected doc type (free text)")
    p.add_argument("--pages", help="comma list of 0-based page indexes to benchmark")
    p.add_argument("--stratum", default="real", choices=("real", "public", "synthetic"))
    p.add_argument("--notes", default="")
    p.set_defaults(func=_cmd_manifest_add)
    p = ms.add_parser("show", help="list manifest rows")
    p.add_argument("filter", nargs="?", default="all",
                   help="all | tag:x | lang:ko | kind:photo | stratum:real | sha:abcd  (',' = OR, '&' = AND)")
    p.set_defaults(func=_cmd_manifest_show)
    p = ms.add_parser("build-synthetic", help="add eval/synthetic/docs with text-layer gold")
    p.set_defaults(func=_cmd_manifest_synth)
    p = ms.add_parser("materialize", help="copy real docs into bench/corpus/ and use relative paths (portable bench/)")
    p.set_defaults(func=_cmd_manifest_materialize)
    p = ms.add_parser("sniff", help="print what the sniffer sees for a file (no manifest change)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=_cmd_manifest_sniff)

    p = sub.add_parser("render", help="pre-render pages into the cache")
    p.add_argument("--docs", dest="filter", default="all")
    p.add_argument("--preset", default="v200")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=_cmd_render)

    p = sub.add_parser("gold", help="generate gold transcripts with Claude (Agent SDK)")
    p.add_argument("--docs", dest="filter", default="all")
    p.add_argument("--model", help="claude model id (default: env MDMDOC_BENCH_GOLD_MODEL or probe)")
    p.add_argument("--probe", action="store_true", help="only check which model works and exit")
    p.add_argument("--force", action="store_true", help="ignore the cache")
    p.add_argument("--single-pass", action="store_true", help="skip the verification pass")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--timeout", type=int, default=600, help="seconds per pass per page")
    p.add_argument("--limit", type=int, default=0, help="stop after N pages (0 = all)")
    p.set_defaults(func=_cmd_gold)
    p = sub.add_parser("gold-review", help="write side-by-side HTML pages for human review")
    p.add_argument("--docs", dest="filter", default="all")
    p.add_argument("-n", type=int, default=12)
    p.add_argument("--all", action="store_true", help="every gold page, not a stratified sample")
    p.set_defaults(func=_cmd_gold_review)
    p = sub.add_parser("gold-accept", help="mark a gold page as human-checked")
    p.add_argument("doc_id")
    p.add_argument("page", type=int)
    p.set_defaults(func=_cmd_gold_accept)
    p = sub.add_parser("gold-fix", help="replace a gold page text with a corrected file")
    p.add_argument("doc_id")
    p.add_argument("page", type=int)
    p.add_argument("--text", required=True, help="path to the corrected transcript (UTF-8)")
    p.set_defaults(func=_cmd_gold_fix)

    p = sub.add_parser("run", help="run candidate engines over the corpus")
    p.add_argument("--engines", required=True, help="comma list of engine specs")
    p.add_argument("--docs", dest="filter", default="all")
    p.add_argument("--tag", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--timeout", type=int, default=0, help="per-page seconds (0 = engine default)")
    p.add_argument("--pages-cap", type=int, default=0, help="max pages per doc (0 = manifest)")
    p.add_argument("--ollama-host", default=None)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("report", help="score results against gold; write leaderboard + diffs")
    p.add_argument("--tag", required=True)
    p.add_argument("--compare", help="previous tag to diff against")
    p.add_argument("--decide", action="store_true", help="also write bench/DECISION.md")
    p.add_argument("--platform", choices=("any", "macos", "windows", "linux", "abap"), default="any",
                   help="only engines that can run on this target (abap = the SAP twin ZMDMDOC)")
    p.add_argument("--no-diffs", action="store_true")
    p.set_defaults(func=_cmd_report)
    p = sub.add_parser("worst", help="worst pages for an engine")
    p.add_argument("--tag", required=True)
    p.add_argument("--engine", required=True)
    p.add_argument("--slice", default="all")
    p.add_argument("-n", type=int, default=5)
    p.set_defaults(func=_cmd_worst)
    p = sub.add_parser("doctype", help="classify doc type from candidate transcripts")
    p.add_argument("--tag", required=True)
    p.add_argument("--engine", default=None, help="only this engine (default: all in tag)")
    p.add_argument("--classifier", default=None, help="ollama text model (default role TEXT)")
    p.set_defaults(func=_cmd_doctype)

    p = sub.add_parser("public", help="fetch public handwriting/form samples into bench/public")
    p.add_argument("--funsd", type=int, default=0, help="N FUNSD test forms")
    p.add_argument("--notebooks-en", type=int, default=0, help="N handwritten EN notebook pages")
    p.add_argument("--notebooks-ru", type=int, default=0, help="N handwritten RU notebook pages")
    p.set_defaults(func=_cmd_public)
    p = sub.add_parser("tag-handwriting", help="tag docs as handwriting when gold says so")
    p.set_defaults(func=_cmd_tag_hw)

    p = sub.add_parser("models", help="model waves: status / pull commands")
    p.add_argument("--wave", type=int, default=1)
    p.add_argument("--pull", action="store_true", help="actually download the wave's missing models")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_models)
    p = sub.add_parser("doctor", help="ollama host, vision capabilities, tesseract, swift, mlx worker")
    p.set_defaults(func=_cmd_doctor)
    p = sub.add_parser("status", help="progress: gold coverage, cells per engine per tag")
    p.set_defaults(func=_cmd_status)
    return ap


def main(argv: list[str]) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)
