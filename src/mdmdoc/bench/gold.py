"""Gold transcripts via the Claude Agent SDK — two independent passes per page.

Pass 1 (gold_transcribe): Claude `Read`s the full page (150 dpi) and zoomed
tiles (300 dpi) and returns the transcription + every label→value pair +
a doc-type guess as structured JSON.
Pass 2 (gold_verify): a fresh session gets the same images plus pass-1 JSON
and returns the corrected JSON + the list of corrections. The final gold is
pass 2; CER(pass1, pass2) is stored as the page's "gold noise".

Storage: bench/gold/<doc_id>/p<idx>.json  (full values — bench/ is gitignored)
         bench/gold/<doc_id>/gold.md       (human-readable)
         bench/gold/review/<doc_id>_p<idx>.html   (side-by-side for Egor)
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..extract import engines as E, render as R
from . import manifest, metrics as M

DOC_TYPES = ["bank_letter", "bank_statement", "bankbook", "voided_check", "payment_instructions",
             "w9", "w8ben", "w8bene", "invoice", "remittance_advice", "business_license",
             "company_registration", "tax_certificate", "id_document", "bank_card", "email",
             "form", "receipt", "contract", "letter", "other"]

_FIELD = {"type": "object",
          "properties": {"label": {"type": "string"}, "value": {"type": "string"},
                         "handwritten": {"type": "boolean"}},
          "required": ["label", "value", "handwritten"], "additionalProperties": False}

GOLD_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": DOC_TYPES},
        "doc_type_free": {"type": "string"},
        "doc_type_confidence": {"type": "number"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string"},
        "fields": {"type": "array", "items": _FIELD},
        "unreadable_count": {"type": "integer"},
        "handwriting_present": {"type": "boolean"},
        "tables_present": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["doc_type", "doc_type_free", "doc_type_confidence", "languages", "text", "fields",
                 "unreadable_count", "handwriting_present", "tables_present", "notes"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = json.loads(json.dumps(GOLD_SCHEMA))
VERIFY_SCHEMA["properties"]["corrections"] = {
    "type": "array",
    "items": {"type": "object",
              "properties": {"before": {"type": "string"}, "after": {"type": "string"},
                             "reason": {"type": "string"}},
              "required": ["before", "after", "reason"], "additionalProperties": False}}
VERIFY_SCHEMA["required"] = VERIFY_SCHEMA["required"] + ["corrections"]

MODEL_CANDIDATES = ["claude-opus-5", "claude-fable-5", "claude-sonnet-5"]
_LOGGED_OUT = ("not logged in", "please run /login", "invalid api key", "authentication_error",
               "oauth token", "error result: success")


class GoldError(RuntimeError):
    pass


class GoldAuthError(GoldError):
    pass


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def looks_logged_out(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _LOGGED_OUT)


def _ensure_cli_path() -> None:
    wanted = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    parts = os.environ.get("PATH", "").split(":")
    missing = [p for p in wanted if p not in parts]
    if missing:
        os.environ["PATH"] = ":".join(missing + parts)


def gold_dir(doc_id: str) -> Path:
    return config.BENCH_DIR / "gold" / doc_id


def gold_path(doc_id: str, page: int) -> Path:
    return gold_dir(doc_id) / f"p{page}.json"


def model_file() -> Path:
    return config.BENCH_DIR / "gold" / ".model"


# ── images shown to Claude ────────────────────────────────────────────────────

def gold_images(doc: manifest.Doc, page: int, layout: str | None = None) -> list[Path]:
    full = R.render_page(doc.abs_path, doc.render_dir, page, R.PRESETS["gold"])
    hi = R.render_page(doc.abs_path, doc.render_dir, page, R.PRESETS["gold300"])
    layout = layout or ("r3x2" if "dense" in doc.tags else "q4")
    return [full] + R.tiles(hi, layout, min_long_side=1400)


# ── one SDK session ───────────────────────────────────────────────────────────

async def _session(model: str, system_prompt: str, user_prompt: str, images: list[Path],
                   schema: dict, timeout: int, max_turns: int = 20) -> tuple[dict, dict]:
    _ensure_cli_path()
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    cwd = images[0].parent
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        tools=["Read"],
        allowed_tools=["Read"],
        disallowed_tools=["Task", "WebFetch", "WebSearch", "Bash", "Write", "Edit", "Glob", "Grep",
                          "NotebookEdit", "TodoWrite"],
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=max_turns,
        cwd=str(cwd),
        output_format={"type": "json_schema", "schema": schema},
        max_buffer_size=32 * 1024 * 1024,   # Read of a page image is a multi-MB message
    )
    result: ResultMessage | None = None

    async def consume():
        nonlocal result
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message

    await asyncio.wait_for(consume(), timeout=timeout)
    if result is None:
        raise GoldError("no ResultMessage from the SDK")
    stats = {"cost_usd": result.total_cost_usd, "duration_s": round((result.duration_ms or 0) / 1000, 1),
             "num_turns": result.num_turns, "session_id": result.session_id,
             "usage": result.usage if isinstance(result.usage, dict) else None}
    if result.is_error:
        txt = str(result.result)[:400]
        if looks_logged_out(txt):
            raise GoldAuthError(f"Claude CLI is not logged in: {txt}")
        raise GoldError(f"session error: {txt}")
    data = result.structured_output
    if not isinstance(data, dict):
        raise GoldError(f"no structured output (stop_reason={result.stop_reason}); "
                        f"result={str(result.result)[:200]}")
    return data, stats


def _image_list(images: list[Path]) -> str:
    return "\n".join(f"{i + 1}. {p}" for i, p in enumerate(images))


def _user_prompt_pass1(doc: manifest.Doc, page: int, images: list[Path]) -> str:
    return (
        f"Document file: {doc.name} — page {page + 1} of {doc.pages_total}.\n"
        f"Use the Read tool to open these image files, IN THIS ORDER. The first is the full page; "
        f"the rest are overlapping zoomed tiles of the same page (reading order: left→right, top→bottom):\n"
        f"{_image_list(images)}\n\n"
        "Read the full page first for layout and reading order, then every tile to verify each character. "
        "Then return the JSON object described by the schema: the complete verbatim transcription in "
        "`text`, every label→value pair in `fields`, the document type, languages (ISO 639-1), "
        "unreadable glyph count, and flags for handwriting/tables. "
        "Put any remark about ambiguous glyphs into `notes`, never into `text`."
    )


def _user_prompt_pass2(doc: manifest.Doc, page: int, images: list[Path], pass1: dict) -> str:
    draft = json.dumps({k: pass1.get(k) for k in ("text", "fields", "doc_type", "doc_type_free",
                                                   "languages", "unreadable_count",
                                                   "handwriting_present", "tables_present")},
                       ensure_ascii=False, indent=1)
    return (
        f"Document file: {doc.name} — page {page + 1} of {doc.pages_total}.\n"
        f"Use the Read tool to open these image files, IN THIS ORDER (full page first, then zoomed tiles):\n"
        f"{_image_list(images)}\n\n"
        "Here is the first reviewer's transcription (JSON):\n```json\n" + draft + "\n```\n\n"
        "Re-read the page against the tiles character by character and return the corrected JSON "
        "(same schema, plus `corrections`). Keep what is right unchanged."
    )


# ── per-page pipeline ─────────────────────────────────────────────────────────

def _cached_ok(existing: dict | None, model: str, p1_tag: str, p2_tag: str, single: bool) -> bool:
    if not existing or existing.get("status") in (None, "error"):
        return False
    if existing.get("status") == "human_checked":
        return True
    if existing.get("model") != model or existing.get("prompt_pass1") != p1_tag:
        return False
    if not single and existing.get("prompt_pass2") != p2_tag:
        return False
    return bool((existing.get("final") or {}).get("text"))


async def gold_page(doc: manifest.Doc, page: int, *, model: str, timeout: int,
                    single_pass: bool = False, force: bool = False, layout: str | None = None) -> dict:
    p1_text, p1_tag, _ = E.resolve_prompt("gold_transcribe")
    p2_text, p2_tag, _ = E.resolve_prompt("gold_verify")
    out_path = gold_path(doc.doc_id, page)
    existing = None
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    if not force and _cached_ok(existing, model, p1_tag, p2_tag, single_pass):
        return dict(existing, _cached=True)
    if existing and existing.get("status") == "human_checked" and not force:
        return dict(existing, _cached=True)
    images = gold_images(doc, page, layout)
    t0 = time.time()
    rec = {"doc_id": doc.doc_id, "doc_name": doc.name, "page": page, "model": model,
           "prompt_pass1": p1_tag, "prompt_pass2": None if single_pass else p2_tag,
           "images": [str(p) for p in images], "ts": _now(), "status": "error"}
    try:
        pass1, s1 = await _session(model, p1_text, _user_prompt_pass1(doc, page, images), images,
                                   GOLD_SCHEMA, timeout)
        rec["pass1"] = pass1
        rec["stats_pass1"] = s1
        final = pass1
        if not single_pass:
            pass2, s2 = await _session(model, p2_text, _user_prompt_pass2(doc, page, images, pass1),
                                       images, VERIFY_SCHEMA, timeout)
            rec["pass2"] = pass2
            rec["stats_pass2"] = s2
            final = {k: v for k, v in pass2.items() if k != "corrections"}
            rec["corrections"] = pass2.get("corrections") or []
            rec["disagreement_cer"] = M.cer(pass1.get("text", ""), pass2.get("text", ""))
        rec["final"] = final
        rec["status"] = "verified" if not single_pass else "auto"
        rec["cost_usd"] = round(sum((rec.get(k) or {}).get("cost_usd") or 0
                                    for k in ("stats_pass1", "stats_pass2")), 4)
    except GoldAuthError:
        raise
    except Exception as e:
        rec["error"] = f"{e.__class__.__name__}: {str(e)[:500]}"
    rec["duration_s"] = round(time.time() - t0, 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write_text(out_path, json.dumps(rec, ensure_ascii=False, indent=1))
    _write_gold_md(doc)
    return rec


def _write_gold_md(doc: manifest.Doc) -> None:
    parts = [f"# Gold — {doc.name}", ""]
    for p in sorted(gold_dir(doc.doc_id).glob("p*.json"), key=lambda x: int(x.stem[1:])):
        try:
            g = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        f = g.get("final") or {}
        parts += [f"## page {g.get('page', 0) + 1} — {f.get('doc_type_free', '')} "
                  f"({g.get('status')}, noise CER {g.get('disagreement_cer')})", "",
                  f.get("text", ""), ""]
        if f.get("fields"):
            parts += ["| label | value | hw |", "|---|---|---|"]
            for fld in f["fields"]:
                parts.append(f"| {str(fld.get('label', '')).replace('|', '\\|')} | "
                             f"{str(fld.get('value', '')).replace('|', '\\|')} | "
                             f"{'✍' if fld.get('handwritten') else ''} |")
            parts.append("")
    (gold_dir(doc.doc_id) / "gold.md").write_text("\n".join(parts), encoding="utf-8")


# ── probe ─────────────────────────────────────────────────────────────────────

async def _probe_model(model: str, timeout: int = 120) -> tuple[bool, str]:
    _ensure_cli_path()
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    options = ClaudeAgentOptions(model=model, tools=[], allowed_tools=[], permission_mode="dontAsk",
                                 setting_sources=[], max_turns=1)
    result = None

    async def consume():
        nonlocal result
        async for m in query(prompt="Reply with exactly: OK", options=options):
            if isinstance(m, ResultMessage):
                result = m
    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    except Exception as e:
        return False, f"{e.__class__.__name__}: {str(e)[:200]}"
    if result is None:
        return False, "no result"
    if result.is_error:
        return False, str(result.result)[:200]
    return True, str(result.result)[:80]


def pick_model(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("MDMDOC_BENCH_GOLD_MODEL")
    if env:
        return env
    mf = model_file()
    if mf.exists():
        m = mf.read_text(encoding="utf-8").strip()
        if m:
            return m
    return MODEL_CANDIDATES[0]


def probe(explicit: str | None = None) -> str | None:
    cands = [explicit] if explicit else MODEL_CANDIDATES
    for m in cands:
        ok, why = asyncio.run(_probe_model(m))
        _log(f"probe {m}: {'ok' if ok else 'FAIL'} — {why}")
        if ok:
            model_file().parent.mkdir(parents=True, exist_ok=True)
            model_file().write_text(m + "\n", encoding="utf-8")
            return m
        if looks_logged_out(why):
            _log("Claude CLI is not logged in — run `claude` and `/login`, then retry")
            return None
    return None


# ── batch ─────────────────────────────────────────────────────────────────────

async def _run_batch(jobs: list[tuple[manifest.Doc, int]], *, model: str, timeout: int,
                     concurrency: int, single_pass: bool, force: bool) -> dict:
    sem = asyncio.Semaphore(max(1, concurrency))
    stats = {"done": 0, "errors": 0, "cached": 0, "cost_usd": 0.0}
    total = len(jobs)

    async def one(i: int, d: manifest.Doc, p: int):
        async with sem:
            rec = await gold_page(d, p, model=model, timeout=timeout, single_pass=single_pass, force=force)
            if rec.get("_cached"):
                stats["cached"] += 1
                return
            if rec.get("error"):
                stats["errors"] += 1
                _log(f"[{i}/{total}] {d.name[:40]} p{p} ERROR {rec['error'][:160]}")
            else:
                stats["done"] += 1
                stats["cost_usd"] += rec.get("cost_usd") or 0
                f = rec.get("final") or {}
                _log(f"[{i}/{total}] {d.name[:40]} p{p} {rec.get('status')} "
                     f"{len(f.get('text', ''))}ch fields={len(f.get('fields') or [])} "
                     f"noise={rec.get('disagreement_cer')} ${rec.get('cost_usd') or 0:.2f} "
                     f"{rec.get('duration_s')}s {f.get('doc_type_free', '')[:40]}")

    await asyncio.gather(*(one(i + 1, d, p) for i, (d, p) in enumerate(jobs)))
    return stats


def cli_gold(a) -> int:
    if a.probe:
        return 0 if probe(a.model) else 1
    model = pick_model(a.model)
    if not model_file().exists() and not a.model and not os.environ.get("MDMDOC_BENCH_GOLD_MODEL"):
        model = probe(None) or model
    docs = manifest.load(a.filter)
    docs = [d for d in docs if d.gold_source != "textlayer"]
    if not docs:
        _log(f"no claude-gold documents match {a.filter!r}")
        return 2
    jobs = [(d, p) for d in sorted(docs, key=lambda d: (0 if "core" in d.tags else 1, d.doc_id))
            for p in d.pages]
    if a.limit:
        jobs = jobs[: a.limit]
    _log(f"gold: model={model} docs={len(docs)} pages={len(jobs)} concurrency={a.concurrency} "
         f"{'single-pass' if a.single_pass else 'two-pass'}")
    try:
        stats = asyncio.run(_run_batch(jobs, model=model, timeout=a.timeout, concurrency=a.concurrency,
                                       single_pass=a.single_pass, force=a.force))
    except GoldAuthError as e:
        _log(str(e))
        _log("→ run `claude` in a terminal, `/login`, then re-run this command (cached pages are kept)")
        return 3
    except KeyboardInterrupt:
        _log("interrupted — finished pages are cached; re-run to resume")
        return 130
    _log(f"gold done: {stats['done']} ok, {stats['errors']} errors, {stats['cached']} cached, ${stats['cost_usd']:.2f}")
    return 0 if stats["errors"] == 0 else 1


# ── human review ──────────────────────────────────────────────────────────────

def _img_tag(path: Path, max_w: int = 760) -> str:
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return f"<img src='data:{mime};base64,{data}' style='max-width:{max_w}px;width:100%;border:1px solid #ccc'>"
    except Exception:
        return f"<p>(image missing: {html.escape(str(path))})</p>"


def review_html(doc: manifest.Doc, page: int, rec: dict) -> str:
    f = rec.get("final") or {}
    images = [Path(p) for p in rec.get("images") or []]
    full = images[0] if images else None
    tiles = images[1:]
    corr = rec.get("corrections") or []
    rows = "".join(f"<tr><td>{html.escape(str(x.get('label', '')))}</td><td>{html.escape(str(x.get('value', '')))}</td>"
                   f"<td>{'✍' if x.get('handwritten') else ''}</td></tr>" for x in f.get("fields") or [])
    corr_html = "".join(f"<li><code>{html.escape(c.get('before', ''))}</code> → <code>{html.escape(c.get('after', ''))}</code>"
                        f" <i>{html.escape(c.get('reason', ''))}</i></li>" for c in corr)
    return f"""<!doctype html><meta charset='utf-8'><title>gold review {html.escape(doc.name)} p{page + 1}</title>
<style>body{{font:14px -apple-system,sans-serif;margin:16px;color:#111}} .wrap{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
pre{{white-space:pre-wrap;font:13px ui-monospace,monospace;background:#fafafa;border:1px solid #ddd;padding:10px}}
table{{border-collapse:collapse;font-size:13px}} td,th{{border:1px solid #ddd;padding:3px 6px;text-align:left}}
.tiles img{{max-width:360px;margin:4px;border:1px solid #ccc}} .meta{{color:#555}}</style>
<h1>{html.escape(doc.name)} — page {page + 1}</h1>
<p class='meta'>doc_id {doc.doc_id} · model {html.escape(str(rec.get('model')))} · status <b>{html.escape(str(rec.get('status')))}</b>
 · noise CER between passes: {rec.get('disagreement_cer')} · type: <b>{html.escape(str(f.get('doc_type_free', '')))}</b>
 ({html.escape(str(f.get('doc_type', '')))}) · languages {html.escape(','.join(f.get('languages') or []))}
 · handwriting {f.get('handwriting_present')} · unreadable {f.get('unreadable_count')}</p>
<p class='meta'>accept: <code>uv run mdmdoc bench gold-accept {doc.doc_id} {page}</code> ·
 fix: <code>uv run mdmdoc bench gold-fix {doc.doc_id} {page} --text corrected.txt</code></p>
<div class='wrap'><div>{_img_tag(full) if full else ''}</div>
<div><h3>transcript (final)</h3><pre>{html.escape(f.get('text', ''))}</pre>
<h3>fields ({len(f.get('fields') or [])})</h3><table><tr><th>label</th><th>value</th><th>hw</th></tr>{rows}</table>
<h3>corrections by pass 2 ({len(corr)})</h3><ul>{corr_html or '<li>none</li>'}</ul>
<p class='meta'>notes: {html.escape(str(f.get('notes', '')))}</p></div></div>
<h3>tiles shown to Claude</h3><div class='tiles'>{''.join(_img_tag(t, 360) for t in tiles)}</div>
"""


def cli_review(a) -> int:
    docs = [d for d in manifest.load(a.filter) if d.gold_source != "textlayer"]
    recs = []
    for d in docs:
        for p in d.pages:
            gp = gold_path(d.doc_id, p)
            if gp.exists():
                try:
                    rec = json.loads(gp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if rec.get("final"):
                    recs.append((d, p, rec))
    if not recs:
        _log("no gold pages yet")
        return 2
    if not a.all:
        # stratified: one per tag bucket first, ordered by disagreement (noisiest first)
        recs.sort(key=lambda r: -(r[2].get("disagreement_cer") or 0))
        picked, seen_tags = [], set()
        for d, p, rec in recs:
            key = tuple(sorted(t for t in d.tags if t in ("cjk", "handwriting", "photo", "rtl", "w9", "w8",
                                                           "statement", "bankbook", "seal")))
            if key not in seen_tags or len(picked) < a.n // 2:
                picked.append((d, p, rec))
                seen_tags.add(key)
            if len(picked) >= a.n:
                break
        for r in recs:
            if len(picked) >= a.n:
                break
            if r not in picked:
                picked.append(r)
        recs = picked
    out_dir = config.BENCH_DIR / "gold" / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    index = ["<!doctype html><meta charset='utf-8'><title>gold review</title><h1>Gold review queue</h1><ol>"]
    for d, p, rec in recs:
        fn = out_dir / f"{d.doc_id}_p{p}.html"
        fn.write_text(review_html(d, p, rec), encoding="utf-8")
        index.append(f"<li><a href='{fn.name}'>{html.escape(d.name)} p{p + 1}</a> — {rec.get('status')} "
                     f"noise {rec.get('disagreement_cer')} · {html.escape(str((rec.get('final') or {}).get('doc_type_free', '')))}</li>")
        print(fn)
    (out_dir / "index.html").write_text("\n".join(index) + "</ol>", encoding="utf-8")
    _log(f"{len(recs)} review page(s) → {out_dir / 'index.html'}")
    return 0


def _set_status(doc_id: str, page: int, status: str, text: str | None = None) -> int:
    d = manifest.get(doc_id)
    if not d:
        _log(f"unknown doc {doc_id}")
        return 2
    gp = gold_path(d.doc_id, page)
    if not gp.exists():
        _log(f"no gold for {d.name} p{page}")
        return 2
    rec = json.loads(gp.read_text(encoding="utf-8"))
    if text is not None:
        rec.setdefault("human_edits", []).append({"ts": _now(), "before": (rec.get("final") or {}).get("text", "")})
        rec.setdefault("final", {})["text"] = text
    rec["status"] = status
    rec["human_ts"] = _now()
    config.atomic_write_text(gp, json.dumps(rec, ensure_ascii=False, indent=1))
    _write_gold_md(d)
    _log(f"{d.name} p{page} → {status}")
    return 0


def cli_accept(a) -> int:
    return _set_status(a.doc_id, a.page, "human_checked")


def cli_fix(a) -> int:
    text = Path(a.text).read_text(encoding="utf-8")
    return _set_status(a.doc_id, a.page, "human_checked", text=text)
