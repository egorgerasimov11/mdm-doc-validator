"""Model waves (what to pull for each benchmark wave) and `bench doctor`."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config, ocr
from ..extract import engines as E, ollama as O

# (kind, name, approx_gb, note)
WAVES: dict[int, list[tuple[str, str, float, str]]] = {
    1: [
        ("ollama", "qwen2.5vl:7b", 6.0, "production VISION role, 128K ctx"),
        ("ollama", "gemma3:4b", 3.3, "vision (tags under-report it)"),
        ("ollama", "gemma4:e2b", 7.2, "vision+tools+thinking 5B"),
        ("ollama", "gemma4:latest", 9.6, "vision+tools+thinking 8B"),
    ],
    2: [
        ("ollama", "qwen3-vl:4b", 3.3, "Qwen3-VL 4B, 256K ctx"),
        ("ollama", "qwen3-vl:8b", 6.1, "Qwen3-VL 8B, 256K ctx"),
        ("ollama", "deepseek-ocr:3b", 6.7, "OCR specialist, 8K ctx (always tiled)"),
        ("mlx", "mlx-community/Qwen3-VL-4B-Instruct-4bit", 3.1, "MLX twin of qwen3-vl:4b"),
        ("mlx", "mlx-community/Qwen3-VL-8B-Instruct-4bit", 5.8, "MLX twin of qwen3-vl:8b"),
        ("mlx", "mlx-community/DeepSeek-OCR-4bit", 2.5, "OCR specialist"),
        ("mlx", "mlx-community/olmOCR-2-7B-1025-4bit", 5.7, "PDF→markdown specialist (scans)"),
        ("mlx", "mlx-community/Nanonets-OCR2-3B-4bit", 3.1, "structured markdown OCR"),
        ("mlx", "mlx-community/dots.ocr-4bit", 3.5, "~100 languages"),
        ("mlx", "mlx-community/PaddleOCR-VL-1.5-4bit", 2.0, "0.9B, handwriting claimed"),
    ],
    3: [
        ("ollama", "minicpm-v:8b", 5.5, "strong Chinese OCR per GB"),
        ("ollama", "granite3.2-vision:2b", 2.4, "documents/tables, 16K ctx"),
        ("ollama", "qwen2.5vl:3b", 3.2, "latency axis"),
    ],
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def hf_cached(repo: str) -> bool:
    home = Path.home() / ".cache" / "huggingface" / "hub" / ("models--" + repo.replace("/", "--"))
    return home.exists() and any((home / "snapshots").glob("*")) if (home / "snapshots").exists() else False


def wave_status(wave: int) -> list[dict]:
    have = O.available_models() if O.alive() else set()
    rows = []
    for kind, name, gb, note in WAVES.get(wave, []):
        present = (name in have) if kind == "ollama" else hf_cached(name)
        rows.append({"kind": kind, "name": name, "gb": gb, "note": note, "present": present})
    return rows


def cli_models(a) -> int:
    rows = wave_status(a.wave)
    missing_gb = sum(r["gb"] for r in rows if not r["present"])
    free_gb = shutil.disk_usage(str(config.PROJECT_ROOT)).free / 1e9
    print(f"wave {a.wave}: {sum(r['present'] for r in rows)}/{len(rows)} present, "
          f"{missing_gb:.1f} GB to download, {free_gb:.0f} GB free on disk")
    for r in rows:
        print(f"  {'✓' if r['present'] else '·'} {r['kind']:6s} {r['name']:48s} {r['gb']:5.1f} GB  {r['note']}")
    todo = [r for r in rows if not r["present"]]
    if not todo:
        return 0
    print("\ncommands:")
    for r in todo:
        if r["kind"] == "ollama":
            print(f"  ollama pull {r['name']}")
        else:
            print(f"  uv run --project tools/mlxvlm python worker.py --model {r['name']} --download-only")
    if not a.pull:
        return 0
    if missing_gb > free_gb - 15:
        _log(f"refusing to pull: {missing_gb:.1f} GB needed, only {free_gb:.0f} GB free (keep 15 GB headroom)")
        return 2
    if not a.yes:
        _log("add --yes to download")
        return 0
    for r in todo:
        _log(f"→ pulling {r['name']} ({r['gb']} GB)")
        if r["kind"] == "ollama":
            last = {"s": ""}

            def prog(status, done, total, last=last):
                if total and done is not None:
                    pct = f"{100 * done / total:5.1f}%"
                    if pct != last["s"]:
                        last["s"] = pct
                        _log(f"   {status} {pct}")
            try:
                O.pull(r["name"], progress=prog)
            except Exception as e:
                _log(f"   pull failed: {e}")
        else:
            wd = config.PROJECT_ROOT / "tools" / "mlxvlm"
            rc = subprocess.call(["uv", "run", "--project", str(wd), "python", "worker.py",
                                  "--model", r["name"], "--download-only"], cwd=str(wd))
            if rc != 0:
                _log(f"   download failed (rc={rc})")
    return 0


def cli_doctor(a) -> int:
    print(f"project root : {config.PROJECT_ROOT}")
    print(f"bench dir    : {config.BENCH_DIR} ({'exists' if config.BENCH_DIR.exists() else 'missing'})")
    print(f"ollama host  : {O.host()} {'(alive)' if O.alive() else '(NOT reachable)'}")
    if O.alive():
        for m in sorted(O.available_models()):
            vis = O.has_vision(m)
            if vis:
                print(f"   vision: {m}  ctx={O.context_length(m)}")
        loaded = [x.get("name") for x in O.ps()]
        print(f"   loaded now: {loaded or 'none'}")
    print(f"tesseract    : {'yes' if ocr.HAVE_TESSERACT else 'NO'}  cjk packs: {ocr.cjk_lang() if ocr.HAVE_TESSERACT else '-'}")
    ok, why = E.ensure_visionocr()
    print(f"apple vision : {'ready' if ok else 'NOT available: ' + why}")
    if ok:
        try:
            out = subprocess.run([str(E.visionocr_binary()), "--info"], capture_output=True, timeout=30)
            info = json.loads(out.stdout.decode("utf-8", "replace"))
            print(f"   document mode: {info.get('document_mode')}  languages: {len(info.get('legacy_languages', []))}")
        except Exception as e:
            print(f"   --info failed: {e}")
    wd = config.PROJECT_ROOT / "tools" / "mlxvlm"
    print(f"mlx worker   : {'synced' if (wd / '.venv').exists() else 'not synced (uv sync --project tools/mlxvlm)'}")
    try:
        import claude_agent_sdk  # noqa: F401
        print(f"claude sdk   : {getattr(claude_agent_sdk, '__version__', 'present')}  claude cli: {shutil.which('claude') or 'NOT in PATH'}")
    except Exception:
        print("claude sdk   : not installed (uv sync --group bench)")
    free_gb = shutil.disk_usage(str(config.PROJECT_ROOT)).free / 1e9
    print(f"disk free    : {free_gb:.0f} GB")
    return 0


def cli_status(a) -> int:
    """Progress overview: gold coverage and cells per engine per tag."""
    import json as _json
    from collections import Counter
    from . import manifest
    from .gold import gold_path
    from .run import load_cell, results_dir
    docs = manifest.load("all")
    by_stratum = Counter(d.stratum for d in docs)
    print(f"corpus: {len(docs)} docs ({dict(by_stratum)}), {sum(len(d.pages) for d in docs)} pages")
    need = [(d, p) for d in docs if d.gold_source != "textlayer" for p in d.pages]
    have = ok = 0
    noise = []
    for d, p in need:
        gp = gold_path(d.doc_id, p)
        if gp.exists():
            try:
                g = _json.loads(gp.read_text(encoding="utf-8"))
            except Exception:
                continue
            have += 1
            if g.get("status") not in (None, "error"):
                ok += 1
                if g.get("disagreement_cer") is not None:
                    noise.append(g["disagreement_cer"])
    med = sorted(noise)[len(noise) // 2] if noise else None
    print(f"gold: {ok}/{len(need)} pages verified ({have - ok} errors); median pass1↔pass2 CER {med}")
    root = config.BENCH_DIR / "results"
    if root.exists():
        for tdir in sorted(p for p in root.iterdir() if p.is_dir()):
            st = {}
            if (tdir / "engines.json").exists():
                st = _json.loads((tdir / "engines.json").read_text(encoding="utf-8"))
            print(f"tag {tdir.name}:")
            for edir in sorted(p for p in tdir.iterdir() if p.is_dir() and p.name != "diffs"):
                loaded = [c for c in (load_cell(p) for p in edir.glob("*/p*.json")) if c]
                errs = sum(1 for c in loaded if c.get("error"))
                eid = next((c.get("engine_id") for c in loaded if c.get("engine_id")), edir.name)
                vers = Counter(str(c.get("engine_version") or "?") for c in loaded)
                vtxt = " ".join(f"v={v}:{n}" for v, n in sorted(vers.items()))
                if len(vers) > 1:
                    vtxt += " ⚠ mixed"
                s = st.get(eid, {})
                print(f"   {eid[:70]:70s} {len(loaded):4d} cells {errs:3d} err  {s.get('state', '')} "
                      f"{('median ' + str(s.get('median_latency_s')) + 's ') if s.get('median_latency_s') else ''}{vtxt}")
    return 0
