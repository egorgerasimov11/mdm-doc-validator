"""Score results against gold → leaderboard, per-doc diffs, history, decision.

Gold for a page comes from bench/gold/<doc_id>/p<idx>.json (field `final`,
written by gold.py) or — for synthetic documents — from the PDF text layer
(exact by construction; fields derived from its "Label: value" lines).
"""
from __future__ import annotations

import difflib
import html
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..extract import engines as E
from . import manifest, metrics as M
from .run import engine_dir, load_cell, results_dir

SLICES = [
    ("real", "stratum:real"),
    ("synthetic", "stratum:synthetic&not:tag:synthetic_cjk_suspect"),
    ("digital", "stratum:real&kind:digital"),
    ("scan", "stratum:real&kind:scan,kind:mixed"),
    ("photo", "stratum:real&kind:photo,tag:photo"),
    ("cjk", "stratum:real&tag:cjk"),
    ("latin", "stratum:real&not:tag:cjk&not:tag:rtl"),
    ("rtl", "stratum:real&tag:rtl"),
    ("handwriting", "tag:handwriting"),
    ("core", "tag:core"),
]
SLICE_KIND = {"photo": "photo", "handwriting": "handwriting"}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── gold access ───────────────────────────────────────────────────────────────

def gold_dir(doc_id: str) -> Path:
    return config.BENCH_DIR / "gold" / doc_id


_KV_RE = re.compile(r"^([^:：]{1,60}?)\s*[:：]\s*(.{1,200})$")


def synthetic_gold(doc: manifest.Doc, page: int) -> dict:
    text = manifest.text_layer(doc, page)
    fields = []
    for ln in text.split("\n"):
        m = _KV_RE.match(ln.strip())
        if m and len(m.group(2).strip()) >= 2:
            fields.append({"label": m.group(1).strip(), "value": m.group(2).strip(), "handwritten": False})
    return {"text": text, "fields": fields, "source": "textlayer", "status": "exact"}


def load_gold(doc: manifest.Doc, page: int) -> dict | None:
    if doc.gold_source == "textlayer":
        return synthetic_gold(doc, page)
    p = gold_dir(doc.doc_id) / f"p{page}.json"
    if not p.exists():
        return None
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    final = g.get("final") or {}
    if not final.get("text"):
        return None
    return {"text": final.get("text", ""), "fields": final.get("fields") or [],
            "source": g.get("model", "claude"), "status": g.get("status", "auto"),
            "disagreement_cer": g.get("disagreement_cer")}


# ── scoring ───────────────────────────────────────────────────────────────────

def score_tag(tag: str, docs: list[manifest.Doc] | None = None) -> dict:
    """→ {engine_id: {"docs": {doc_id: {pages: {p: score}, agg}}, "status": …}}"""
    docs = docs if docs is not None else manifest.load("all")
    by_id = {d.doc_id: d for d in docs}
    rdir = results_dir(tag)
    status = {}
    if (rdir / "engines.json").exists():
        status = json.loads((rdir / "engines.json").read_text(encoding="utf-8"))
    out: dict = {}
    for edir in sorted(p for p in rdir.iterdir() if p.is_dir() and p.name != "diffs"):
        engine_id = None
        docs_out: dict = {}
        for ddir in sorted(p for p in edir.iterdir() if p.is_dir()):
            d = by_id.get(ddir.name)
            if not d:
                continue
            pages: dict = {}
            for cp in sorted(ddir.glob("p*.json")):
                cell = load_cell(cp)
                if not cell:
                    continue
                engine_id = engine_id or cell.get("engine_id")
                page = int(cell["page"])
                gold = load_gold(d, page)
                if gold is None:
                    continue
                cand = cell.get("text") or ""
                sc = M.score_page(gold["text"], gold["fields"], cand)
                sc["latency_s"] = cell.get("latency_s")
                sc["error"] = cell.get("error")
                sc["gold_status"] = gold.get("status")
                pages[page] = sc
            if pages:
                agg = M.aggregate_pages(list(pages.values()))
                agg["doc_id"] = d.doc_id
                agg["doc_name"] = d.name
                docs_out[d.doc_id] = {"pages": pages, "agg": agg}
        if engine_id:
            out[engine_id] = {"docs": docs_out, "status": status.get(engine_id, {})}
    out.update(_virtual_layer_engines(tag, out, by_id))
    return out


def _virtual_layer_engines(tag: str, scored: dict, by_id: dict) -> dict:
    """`layer>E`: the extractor's merge policy evaluated from cached cells — the PDF
    text layer when it is present and plausible, otherwise engine E's output."""
    if "textlayer" not in scored:
        return {}
    tl_dir = engine_dir(tag, "textlayer")
    virt: dict = {}
    for eid, data in scored.items():
        if eid == "textlayer" or eid.startswith("layer>") or eid.startswith("layer+"):
            continue
        e_dir = engine_dir(tag, eid)
        docs_out: dict = {}
        docs_union: dict = {}
        for did, v in data["docs"].items():
            d = by_id[did]
            pages: dict = {}
            pages_union: dict = {}
            for page, sc in v["pages"].items():
                tl = load_cell(tl_dir / did / f"p{page}.json") or {}
                usable = bool((tl.get("meta") or {}).get("usable"))
                ecell = load_cell(e_dir / did / f"p{page}.json") or {}
                gold = load_gold(d, page)
                if gold is None:
                    continue
                if usable:
                    s2 = M.score_page(gold["text"], gold["fields"], tl.get("text") or "")
                    s2["latency_s"] = tl.get("latency_s")
                    s2["error"] = None
                    s2["source"] = "textlayer"
                    pages[page] = s2
                    # union: layer first, then whatever the engine saw that the layer lacks
                    # (handwritten entries, stamps, image-only numbers)
                    union = E.merge_tile_texts([tl.get("text") or "", ecell.get("text") or ""])
                    s3 = M.score_page(gold["text"], gold["fields"], union)
                    s3["latency_s"] = ecell.get("latency_s")
                    s3["error"] = None
                    s3["source"] = "textlayer+" + eid
                    pages_union[page] = s3
                else:
                    pages[page] = dict(sc, source=eid)
                    pages_union[page] = dict(sc, source=eid)
            if pages:
                agg = M.aggregate_pages(list(pages.values()))
                agg["doc_id"] = did
                agg["doc_name"] = d.name
                docs_out[did] = {"pages": pages, "agg": agg}
                agg_u = M.aggregate_pages(list(pages_union.values()))
                agg_u["doc_id"] = did
                agg_u["doc_name"] = d.name
                docs_union[did] = {"pages": pages_union, "agg": agg_u}
        if docs_out:
            virt[f"layer>{eid}"] = {"docs": docs_out, "status": {"state": "virtual"}}
            virt[f"layer+{eid}"] = {"docs": docs_union, "status": {"state": "virtual"}}
    return virt


def slice_table(scored: dict, docs: list[manifest.Doc]) -> dict:
    """→ {slice_name: {engine_id: agg}}"""
    by_id = {d.doc_id: d for d in docs}
    table: dict = {}
    for sname, expr in SLICES:
        ids = {d.doc_id for d in docs if manifest.matches(d, expr)}
        if not ids:
            continue
        table[sname] = {}
        for eid, data in scored.items():
            aggs = [v["agg"] for did, v in data["docs"].items() if did in ids]
            if not aggs:
                continue
            agg = M.aggregate_docs(aggs)
            lats = [p["latency_s"] for v in data["docs"].values() if v["agg"]["doc_id"] in ids
                    for p in v["pages"].values() if p.get("latency_s") is not None]
            agg["median_latency_s"] = round(statistics.median(lats), 2) if lats else None
            agg["p90_latency_s"] = round(sorted(lats)[int(len(lats) * 0.9) - 1], 2) if len(lats) >= 2 else agg["median_latency_s"]
            ok, fails = M.passes(SLICE_KIND.get(sname, "print"), agg)
            agg["pass"] = ok
            agg["fails"] = fails
            table[sname][eid] = agg
    return table


def _fmt(v, pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def leaderboard_md(tag: str, table: dict, scored: dict, docs: list[manifest.Doc]) -> str:
    lines = [f"# Leaderboard — tag `{tag}`", "",
             f"_generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_", "",
             "Ranking is lexicographic: worst-doc field recall → worst-doc entity recall → CER → latency. "
             "`field` = every gold label→value must appear in the transcript; `entity` = digit runs and "
             "IBAN/SWIFT/EIN/SSN (multiset); `line` = fuzzy line recall; CER secondary.", ""]
    for sname, engines in table.items():
        if not engines:
            continue
        lines.append(f"## slice: {sname}")
        lines.append("")
        lines.append("| engine | docs | field recall (worst · macro) | hw-field recall (worst · macro) | entity recall (worst · macro) | CER | line recall | median lat | pass | worst doc |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        ranked = sorted(engines.items(), key=lambda kv: (
            -(kv[1].get("field_recall_worst") or 0), -(kv[1].get("entity_recall_worst") or 0),
            kv[1].get("cer") if kv[1].get("cer") is not None else 9, kv[1].get("median_latency_s") or 0))
        for eid, a in ranked:
            worst = a.get("field_recall_worst_doc") or ""
            wname = next((d.name for d in docs if d.doc_id == worst), worst)
            lines.append(
                f"| `{eid}` | {a['docs']} | {_fmt(a.get('field_recall_worst'), True)} · {_fmt(a.get('field_recall'), True)} "
                f"| {_fmt(a.get('field_hw_recall_worst'), True)} · {_fmt(a.get('field_hw_recall'), True)} "
                f"| {_fmt(a.get('entity_recall_worst'), True)} · {_fmt(a.get('entity_recall'), True)} "
                f"| {_fmt(a.get('cer'))} | {_fmt(a.get('line_recall'), True)} | {_fmt(a.get('median_latency_s'))}s "
                f"| {'✅' if a.get('pass') else '❌'} | {wname[:38]} |")
        lines.append("")
    # engine status
    lines.append("## engine status")
    lines.append("")
    for eid, data in scored.items():
        st = data.get("status") or {}
        lines.append(f"- `{eid}`: {st.get('state', '?')} errors={st.get('errors', 0)} "
                     f"median={st.get('median_latency_s')}s")
    rdir = results_dir(tag)
    if (rdir / "engines.json").exists():
        for eid, st in json.loads((rdir / "engines.json").read_text(encoding="utf-8")).items():
            if eid not in scored:
                lines.append(f"- `{eid}`: {st.get('state')} — {st.get('reason', '')}")
    lines.append("")
    return "\n".join(lines)


# ── diffs ─────────────────────────────────────────────────────────────────────

def write_diffs(tag: str, scored: dict, docs: list[manifest.Doc]) -> int:
    by_id = {d.doc_id: d for d in docs}
    n = 0
    for eid, data in scored.items():
        ddir = results_dir(tag) / "diffs" / E.safe_id(eid)
        ddir.mkdir(parents=True, exist_ok=True)
        for did, v in data["docs"].items():
            d = by_id[did]
            parts_html = []
            parts_md = [f"# {d.name} — `{eid}`", ""]
            for page, sc in sorted(v["pages"].items()):
                gold = load_gold(d, page) or {"text": "", "fields": []}
                cell = load_cell(engine_dir(tag, eid) / did / f"p{page}.json") or {}
                g_lines = M.lines_of(gold["text"])
                c_lines = M.lines_of(cell.get("text") or "")
                hd = difflib.HtmlDiff(wrapcolumn=90)
                table = hd.make_table(g_lines, c_lines, fromdesc=f"gold p{page}", todesc=f"{eid} p{page}", context=False)
                miss_f = sc["field"]["missing"]
                miss_e = sc["entity"]["missing"]
                head = (f"<h2>page {page} — CER {sc['cer']:.3f} · field {sc['field']['recall']:.2f} "
                        f"({sc['field']['found']}/{sc['field']['total']}) · entity {sc['entity']['recall']:.2f} "
                        f"({sc['entity']['found']}/{sc['entity']['total']}) · line {sc['line_recall']:.2f}</h2>")
                lists = ""
                if miss_f:
                    lists += "<p><b>missing field values:</b><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in miss_f) + "</ul></p>"
                if miss_e:
                    lists += "<p><b>missing entities:</b> " + html.escape(", ".join(miss_e)) + "</p>"
                if sc.get("unmatched_gold_lines"):
                    lists += "<p><b>gold lines not found:</b><ul>" + "".join(
                        f"<li>{html.escape(x)}</li>" for x in sc["unmatched_gold_lines"][:25]) + "</ul></p>"
                parts_html.append(head + lists + table)
                parts_md += [f"## page {page}", "",
                             f"CER {sc['cer']:.3f} · field {sc['field']['recall']:.2f} · entity {sc['entity']['recall']:.2f} · line {sc['line_recall']:.2f}", ""]
                if miss_f:
                    parts_md += ["**missing field values:**"] + [f"- {x}" for x in miss_f] + [""]
                if miss_e:
                    parts_md += ["**missing entities:** " + ", ".join(miss_e), ""]
                parts_md += ["| gold | candidate |", "|---|---|"]
                for a, b in zip(g_lines + [""] * max(0, len(c_lines) - len(g_lines)),
                                c_lines + [""] * max(0, len(g_lines) - len(c_lines))):
                    parts_md.append(f"| {a.replace('|', '\\|')} | {b.replace('|', '\\|')} |")
                parts_md.append("")
            page_html = ("<!doctype html><meta charset='utf-8'><title>diff</title>"
                         "<style>body{font:13px -apple-system,sans-serif;margin:16px} table.diff{font:12px ui-monospace,monospace;border-collapse:collapse;width:100%}"
                         ".diff_header{background:#eee} td.diff_header{text-align:right} .diff_next{background:#ddd}"
                         ".diff_add{background:#cfc} .diff_chg{background:#ffd} .diff_sub{background:#fcc}</style>"
                         f"<h1>{html.escape(d.name)} — <code>{html.escape(eid)}</code></h1>" + "".join(parts_html))
            (ddir / f"{did}.html").write_text(page_html, encoding="utf-8")
            (ddir / f"{did}.md").write_text("\n".join(parts_md), encoding="utf-8")
            n += 1
    return n


# ── worst pages ───────────────────────────────────────────────────────────────

def worst_pages(scored: dict, engine_id: str, docs: list[manifest.Doc], slice_expr: str = "all",
                n: int = 5) -> list[dict]:
    data = scored.get(engine_id)
    if not data:
        return []
    ids = {d.doc_id for d in docs if manifest.matches(d, slice_expr)}
    rows = []
    for did, v in data["docs"].items():
        if did not in ids:
            continue
        for page, sc in v["pages"].items():
            rows.append({"doc_id": did, "doc_name": v["agg"]["doc_name"], "page": page,
                         "field_recall": sc["field"]["recall"], "entity_recall": sc["entity"]["recall"],
                         "cer": sc["cer"], "line_recall": sc["line_recall"],
                         "missing_fields": sc["field"]["missing"][:5], "missing_entities": sc["entity"]["missing"][:5],
                         "error": sc.get("error")})
    rows.sort(key=lambda r: (r["field_recall"], r["entity_recall"], -r["cer"]))
    return rows[:n]


# ── history / decision ────────────────────────────────────────────────────────

def append_history(tag: str, table: dict) -> None:
    hp = config.BENCH_DIR / "history.jsonl"
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tag": tag, "slices": {}}
    for sname, engines in table.items():
        row["slices"][sname] = {eid: {k: a.get(k) for k in ("docs", "field_recall_worst", "field_recall",
                                                          "entity_recall_worst", "entity_recall", "cer",
                                                          "line_recall", "median_latency_s", "pass")}
                                for eid, a in engines.items()}
    with hp.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def decision_md(tag: str, table: dict) -> str:
    real = table.get("real", {})
    hw = table.get("handwriting", {})
    lines = [f"# Decision — tag `{tag}`", ""]
    if not real:
        return "\n".join(lines + ["no real-corpus results yet"])
    ranked = sorted(real.items(), key=lambda kv: (
        -(kv[1].get("field_recall_worst") or 0), -(kv[1].get("entity_recall_worst") or 0),
        kv[1].get("cer") if kv[1].get("cer") is not None else 9, kv[1].get("median_latency_s") or 0))
    best_id, best = ranked[0]
    print_ok = all(table.get(s, {}).get(best_id, {}).get("pass", False)
                   for s in ("digital", "scan", "photo", "cjk", "latin", "rtl") if table.get(s, {}).get(best_id))
    lat_ok = (best.get("median_latency_s") or 0) <= 90
    hw_ok = bool(hw.get(best_id, {}).get("pass")) if hw else False
    lines += [f"Best engine on the real corpus: `{best_id}`", "",
              f"- worst-doc field recall: {_fmt(best.get('field_recall_worst'), True)} (macro {_fmt(best.get('field_recall'), True)})",
              f"- worst-doc entity recall: {_fmt(best.get('entity_recall_worst'), True)} (macro {_fmt(best.get('entity_recall'), True)})",
              f"- CER {_fmt(best.get('cer'))}, line recall {_fmt(best.get('line_recall'), True)}, median latency {_fmt(best.get('median_latency_s'))}s",
              f"- print slices pass: {'yes' if print_ok else 'no'}; handwriting pass: {'yes' if hw_ok else 'no' if hw else 'n/a'}; latency ≤ 90 s: {'yes' if lat_ok else 'no'}",
              ""]
    if print_ok and hw_ok and lat_ok:
        verdict = "IMPLEMENT — a local engine meets the print and handwriting thresholds."
    elif print_ok and lat_ok:
        verdict = ("HYBRID — print documents can be extracted locally; handwritten pages must be flagged "
                   "for human/remote transcription.")
    else:
        verdict = "NO-GO (for now) — see the leaderboard for which slices fail and by how much."
    lines += [f"**{verdict}**", ""]
    lines.append("## failing thresholds per slice (best engine)")
    for sname, engines in table.items():
        a = engines.get(best_id)
        if a and a.get("fails"):
            lines.append(f"- {sname}: " + "; ".join(a["fails"]))
    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_report(a) -> int:
    docs = manifest.load("all")
    scored = score_tag(a.tag, docs)
    if not scored:
        _log(f"no scored cells for tag {a.tag!r} (no results, or no gold yet)")
        return 2
    table = slice_table(scored, docs)
    md = leaderboard_md(a.tag, table, scored, docs)
    out = config.BENCH_DIR / "leaderboard.md"
    config.atomic_write_text(out, md)
    (results_dir(a.tag) / "leaderboard.md").write_text(md, encoding="utf-8")
    (results_dir(a.tag) / "scores.json").write_text(
        json.dumps({"table": table, "engines": {eid: {did: v["agg"] for did, v in data["docs"].items()}
                                                for eid, data in scored.items()}},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    n = 0 if a.no_diffs else write_diffs(a.tag, scored, docs)
    append_history(a.tag, table)
    if a.decide:
        dm = decision_md(a.tag, table)
        config.atomic_write_text(config.BENCH_DIR / "DECISION.md", dm)
        print(dm)
    print(md)
    _log(f"leaderboard → {out}; {n} diff file(s) under {results_dir(a.tag) / 'diffs'}")
    return 0


def cli_worst(a) -> int:
    docs = manifest.load("all")
    scored = score_tag(a.tag, docs)
    eid = a.engine
    if eid not in scored:
        cands = [k for k in scored if k.startswith(eid)]
        if len(cands) == 1:
            eid = cands[0]
        else:
            _log(f"engine {a.engine!r} not in tag {a.tag}; have: {list(scored)}")
            return 2
    for r in worst_pages(scored, eid, docs, a.slice, a.n):
        print(f"{r['doc_name'][:45]:45s} p{r['page']}  field {r['field_recall']:.2f}  entity {r['entity_recall']:.2f}  "
              f"CER {r['cer']:.3f}  line {r['line_recall']:.2f}  {'ERROR ' + r['error'] if r['error'] else ''}")
        for x in r["missing_fields"]:
            print(f"      missing field: {x}")
        if r["missing_entities"]:
            print(f"      missing entities: {', '.join(r['missing_entities'])}")
        print(f"      diff: {results_dir(a.tag) / 'diffs' / E.safe_id(eid) / (r['doc_id'] + '.html')}")
    return 0
