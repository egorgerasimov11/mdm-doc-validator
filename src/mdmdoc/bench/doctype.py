"""Document-type classification over candidate transcripts (text-only local model).

Answers a separate question from transcription quality: given what an engine
read, can a small local text model name the document type? Accuracy is
measured against the gold doc_type (closed category) and reported per engine.
"""
from __future__ import annotations

import json
import os
import sys

from .. import model_client as mc
from ..extract import engines as E, ollama as O
from . import manifest
from .gold import gold_path
from .run import engine_dir, load_cell, results_dir


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def classify_text(text: str, filename: str, scripts: list[str], prior: str = "",
                  model: str | None = None) -> dict:
    tmpl, _, _ = E.resolve_prompt("doctype")
    prompt = (tmpl.replace("{filename}", filename).replace("{scripts}", ", ".join(scripts) or "unknown")
              .replace("{prior}", prior or "none").replace("{n}", "1")
              .replace("{transcript}", (text or "")[:6000]))
    role = model or "TEXT"
    try:
        obj, ok = mc.generate_json(role, prompt, options={"num_ctx": 8192, "num_predict": 400,
                                                         "temperature": 0, "seed": 7}, think=False)
    except Exception as e:
        return {"doc_type": "", "doc_type_category": "", "confidence": 0.0, "error": str(e)[:200]}
    if not isinstance(obj, dict):
        return {"doc_type": "", "doc_type_category": "", "confidence": 0.0, "error": "no json"}
    return obj


def cli_doctype(a) -> int:
    docs = manifest.load("all")
    by_id = {d.doc_id: d for d in docs}
    rdir = results_dir(a.tag)
    out_path = rdir / "doctype.json"
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    # the classifier runs on the SAME local Ollama as the vision engines (never the mini tunnel)
    os.environ.setdefault("MDMDOC_OLLAMA_HOST", O.host())
    mc.reset_host()
    try:
        mc.preflight()
    except mc.OllamaUnavailable as e:
        _log(str(e))
        return 3
    for edir in sorted(p for p in rdir.iterdir() if p.is_dir() and p.name != "diffs"):
        if a.engine and E.safe_id(a.engine) != edir.name and not edir.name.startswith(E.safe_id(a.engine)):
            continue
        eid = None
        for ddir in sorted(p for p in edir.iterdir() if p.is_dir()):
            d = by_id.get(ddir.name)
            if not d:
                continue
            cell = load_cell(ddir / f"p{d.pages[0]}.json")
            if not cell or cell.get("error"):
                continue
            eid = eid or cell.get("engine_id")
            key = f"{eid}|{d.doc_id}"
            if key in results and not getattr(a, "force", False):
                continue
            gold = None
            if d.gold_source == "textlayer":
                gold = {"doc_type": d.expected_doc_type}
            else:
                gp = gold_path(d.doc_id, d.pages[0])
                if gp.exists():
                    g = json.loads(gp.read_text(encoding="utf-8")).get("final") or {}
                    gold = {"doc_type": g.get("doc_type"), "doc_type_free": g.get("doc_type_free")}
            pred = classify_text(cell.get("text") or "", d.name, d.scripts, model=a.classifier)
            results[key] = {"engine": eid, "doc_id": d.doc_id, "doc_name": d.name,
                            "pred": pred, "gold": gold,
                            "match": bool(gold and gold.get("doc_type") and
                                          pred.get("doc_type_category") == gold.get("doc_type"))}
            _log(f"[{eid}] {d.name[:40]}: {pred.get('doc_type_category')} / {pred.get('doc_type')!s:50.50} "
                 f"gold={gold.get('doc_type') if gold else '?'} {'✓' if results[key]['match'] else '✗'}")
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    # summary per engine
    per: dict = {}
    for r in results.values():
        if r["gold"] and r["gold"].get("doc_type"):
            s = per.setdefault(r["engine"], [0, 0])
            s[1] += 1
            s[0] += int(r["match"])
    for eid, (hit, tot) in sorted(per.items()):
        print(f"{eid}: doc-type accuracy {hit}/{tot} = {hit / tot:.2f}")
    return 0
