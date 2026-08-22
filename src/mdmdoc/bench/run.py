"""Run candidate engines over the corpus — resumable, one engine at a time.

Cell = (engine.id, engine.version, doc_id, page) →
    bench/results/<tag>/<engine>/<doc_id>/p<idx>.json
Existing non-error cells with the same engine.version are skipped (resume);
--force redoes them. Engines run strictly sequentially (setup → all pages →
teardown in `finally`), so at most one model is resident. Per-page failures
become error cells; five consecutive errors mark the engine broken for this
tag. The runner only ever stops processes it started itself.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..extract import engines as E
from . import manifest


def results_dir(tag: str) -> Path:
    return config.BENCH_DIR / "results" / tag


def engine_dir(tag: str, engine_id: str) -> Path:
    return results_dir(tag) / E.safe_id(engine_id)


def cell_path(tag: str, engine_id: str, doc_id: str, page: int) -> Path:
    return engine_dir(tag, engine_id) / doc_id / f"p{page}.json"


def load_cell(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _now() -> str:
    # microseconds: report.py picks the newest engine_version by `ts`, and two runs
    # can land in the same second
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _order(docs: list[manifest.Doc]) -> list[manifest.Doc]:
    return sorted(docs, key=lambda d: (0 if "core" in d.tags else 1, d.stratum != "real", d.doc_id))


def run_sweep(engines: list[E.PageEngine], docs: list[manifest.Doc], tag: str, *,
              force: bool = False, timeout: int = 0, pages_cap: int = 0,
              max_consecutive_errors: int = 5, log=_log) -> dict:
    rdir = results_dir(tag)
    rdir.mkdir(parents=True, exist_ok=True)
    status_path = rdir / "engines.json"
    status: dict = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            status = {}
    progress = rdir / "progress.log"
    docs = _order(docs)
    summary: dict = {"tag": tag, "engines": {}}

    for eng in engines:
        ok, why = eng.available()
        if not ok:
            log(f"[{eng.id}] unavailable: {why}")
            status[eng.id] = {"state": "unavailable", "reason": why, "ts": _now()}
            summary["engines"][eng.id] = {"state": "unavailable", "reason": why}
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
            continue
        jobs: list[tuple[manifest.Doc, int]] = []
        skipped = 0
        for d in docs:
            pages = d.pages[:pages_cap] if pages_cap else d.pages
            for p in pages:
                cp = cell_path(tag, eng.id, d.doc_id, p)
                if not force and cp.exists():
                    c = load_cell(cp)
                    if c and not c.get("error") and c.get("engine_version") == eng.version:
                        skipped += 1
                        continue
                jobs.append((d, p))
        log(f"[{eng.id}] {len(jobs)} page(s) to run, {skipped} cached")
        summary["engines"][eng.id] = {"state": "running", "todo": len(jobs), "cached": skipped,
                                      "done": 0, "errors": 0, "latency_s": []}
        if not jobs:
            status[eng.id] = {"state": "complete", "ts": _now(), "version": eng.version}
            summary["engines"][eng.id]["state"] = "complete"
            continue
        t_setup = time.time()
        try:
            eng.setup()
        except Exception as e:
            log(f"[{eng.id}] setup failed: {e}")
            status[eng.id] = {"state": "broken", "reason": f"setup: {e}", "ts": _now()}
            summary["engines"][eng.id].update(state="broken", reason=str(e))
            try:
                eng.teardown()
            except Exception:
                pass
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
            continue
        setup_s = round(time.time() - t_setup, 1)
        consecutive = 0
        broken = False
        try:
            for i, (d, p) in enumerate(jobs, 1):
                job = E.PageJob(doc_id=d.doc_id, src=d.abs_path, page=p, cache_dir=d.render_dir,
                                hints={"scripts": list(d.scripts), "langs": list(d.langs)},
                                timeout_s=timeout or eng.default_timeout_s)
                t0 = time.time()
                try:
                    res = eng.transcribe(job)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    res = E.PageResult(error=f"{e.__class__.__name__}: {str(e)[:500]}",
                                       latency_s=round(time.time() - t0, 2))
                cell = {"engine_id": eng.id, "engine_version": eng.version, "doc_id": d.doc_id,
                        "doc_name": d.name, "page": p, "ts": _now(), **res.as_dict()}
                cp = cell_path(tag, eng.id, d.doc_id, p)
                cp.parent.mkdir(parents=True, exist_ok=True)
                config.atomic_write_text(cp, json.dumps(cell, ensure_ascii=False, indent=1))
                with (rdir / "cells.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({k: v for k, v in cell.items() if k not in ("lines", "markdown")},
                                       ensure_ascii=False) + "\n")
                head = (res.text or "").replace("\n", " ⏎ ")[:70]
                line = (f"{_now()} [{eng.id}] {i}/{len(jobs)} {d.name[:40]} p{p} "
                        f"{res.latency_s:.1f}s {len(res.text or '')}ch "
                        f"{'ERROR ' + (res.error or '')[:80] if res.error else head}")
                log(line)
                with progress.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                s = summary["engines"][eng.id]
                s["done"] += 1
                if res.error:
                    s["errors"] += 1
                    consecutive += 1
                    if consecutive >= max_consecutive_errors:
                        broken = True
                        log(f"[{eng.id}] {consecutive} consecutive errors — marking broken for tag {tag}")
                        break
                else:
                    consecutive = 0
                    s["latency_s"].append(res.latency_s)
        finally:
            try:
                eng.teardown()
            except Exception as e:
                log(f"[{eng.id}] teardown: {e}")
        s = summary["engines"][eng.id]
        lat = sorted(s.pop("latency_s"))
        s["median_latency_s"] = lat[len(lat) // 2] if lat else None
        s["state"] = "broken" if broken else "complete"
        status[eng.id] = {"state": s["state"], "ts": _now(), "version": eng.version,
                          "setup_s": setup_s, "median_latency_s": s["median_latency_s"],
                          "errors": s["errors"], "done": s["done"]}
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def cli_run(a) -> int:
    docs = manifest.load(a.filter)
    if not docs:
        _log(f"no documents match {a.filter!r}")
        return 2
    try:
        engines = E.parse_many(a.engines, host=a.ollama_host)
    except (ValueError, FileNotFoundError) as e:
        _log(f"engine spec error: {e}")
        return 2
    _log(f"tag={a.tag} docs={len(docs)} pages={sum(len(d.pages) for d in docs)} engines={[e.id for e in engines]}")
    try:
        summary = run_sweep(engines, docs, a.tag, force=a.force, timeout=a.timeout,
                            pages_cap=a.pages_cap)
    except KeyboardInterrupt:
        _log("interrupted — partial results are kept; re-run the same command to resume")
        return 130
    for eid, s in summary["engines"].items():
        _log(f"  {eid}: {s.get('state')} done={s.get('done', 0)} errors={s.get('errors', 0)} "
             f"median={s.get('median_latency_s')}s {s.get('reason', '')}")
    return 0
