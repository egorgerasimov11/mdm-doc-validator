#!/usr/bin/env python3
"""
doctype_profiles.py — the document-type PATTERN MEMORY (F5).

Egor's ask: the pipeline should "remember what documents look like" the way a
human does — from the documents he already fed it and from his live feedback
(teach-type, Mark valid, 👍) — WITHOUT slowing confident analyses down and
without inventing new document types.

Mechanics: one PII-free-ish profile row per (document, source) in
dataset/doctype_profiles.jsonl — the doc type, the page-marker signature the
perception already computed, and a text embedding of page 1 (nomic-embed via
the idle EMBED role; embedding vectors are stored locally, gitignored, and
never enter run artifacts). At RUN time the memory acts as a PRIOR with three
effort-scaled evidence tiers:

  markers vote      — zero model calls (deterministic engine / effort 1);
  text embedding    — ONE embed call, only when the pipeline is UNCERTAIN
                      (effort ≥ 2; the weak `type_hint or "other"` fallback);
  vision descriptor — ONE short qwen2.5vl layout description, embedded and
                      matched against vision-kind prototypes (effort ≥ 4,
                      only when the text tiers stayed inconclusive).

Safety: the prior fills ONLY the weak fallback — a valid model/strong answer
is never overridden (at most flagged doc_type_uncertain); deterministic
overrides and packet guards always win; the CLOSED set of known types is
enforced (never invents a type); eval gates it off (runctl doctype_prior).
"""
from __future__ import annotations

import hashlib
import json
import threading

from . import config, model_client as mc, runstore

PATH_NAME = "doctype_profiles.jsonl"
_LOCK = threading.Lock()

# match thresholds (embedding cosine): accept when the best same-type score
# clears MIN_SIM, beats the best OTHER type by MIN_MARGIN, and at least
# MIN_SUPPORT distinct documents back the winner (one taught doc must not steer)
MIN_SIM = 0.82
MIN_MARGIN = 0.05
MIN_SUPPORT = 2
MARKERS_MIN_DOCS = 3     # markers vote: unanimous across >=3 distinct documents

DESCRIBE_PROMPT = (
    "Describe this document page's layout and type signals in at most 40 words: "
    "letterhead, logos, tables, form boxes, stamps, signature areas, headers. "
    "Do not transcribe values.")

_EMB_CACHE: dict[str, list[float]] = {}
_EMB_CACHE_CAP = 64


def _path():
    return config.DATASET_DIR / PATH_NAME


def load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def load_for(doc_class: str) -> list[dict]:
    return [r for r in load() if r.get("doc_class") == doc_class]


def _known_types(doc_class: str) -> tuple:
    from .fields import BANK_DOC_TYPES, W9_DOC_TYPES
    return BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES


def _markers_sig(stage_a: dict) -> dict:
    """The page-marker signature perception already computed — profile side."""
    return {"bank_letter": bool(stage_a.get("bank_letter_pages")),
            "invoice": bool(stage_a.get("invoice_pages")),
            "w9_form": bool(stage_a.get("w9_pages")),
            "type_hint": str(stage_a.get("type_hint") or "")}


def _raw_sig(raw) -> dict:
    """The same signature from the LIVE RawDoc (run side)."""
    return {"bank_letter": bool(getattr(raw, "bank_letter_pages", None)),
            "invoice": bool(getattr(raw, "invoice_pages", None)),
            "w9_form": bool(getattr(raw, "w9_pages", None)),
            "type_hint": str(getattr(raw, "type_hint", "") or "")}


def _page1_text_from_file(path: str) -> str:
    """Page-1 text of the ORIGINAL document (run artifacts only keep a masked
    excerpt, which must never be embedded). Text layer only — cheap."""
    try:
        import fitz
        with fitz.open(path) as doc:
            if doc.page_count:
                return doc[0].get_text() or ""
    except Exception:
        pass
    return ""


def _embed_cached(key: str, text: str) -> list[float]:
    if key in _EMB_CACHE:
        return _EMB_CACHE[key]
    vec = (mc.embed([text]) or [[]])[0]
    if len(_EMB_CACHE) >= _EMB_CACHE_CAP:
        _EMB_CACHE.pop(next(iter(_EMB_CACHE)))
    _EMB_CACHE[key] = vec
    return vec


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- capture -----
def capture(run_id: str, source: str, doc_type: str = "",
            with_vision: bool = False) -> dict | None:
    """Add one profile from a stored run (teach-type / mark-valid / 👍 / study).
    Idempotent per (run_id, source). The doc type comes from the operator's
    label when there is one, else the caller's value, else the stored report.
    Unknown/closed-set violations are skipped — the memory never invents types."""
    rows = load()
    if any(r.get("run_id") == run_id and r.get("source") == source for r in rows):
        return None
    meta = runstore.load(run_id, "meta.json") or {}
    stage_a = runstore.load(run_id, "stage_a.json") or {}
    rep = runstore.load(run_id, "report.json") or {}
    if not meta:
        return None
    doc_class = meta.get("doc_class", "bank")
    if not doc_type:
        from .dataset import load_labels
        lab = next((l for l in load_labels() if l.get("doc_sha256") == run_id), None)
        doc_type = (lab or {}).get("doc_type_gold") or rep.get("doc_type", "")
    if doc_type not in _known_types(doc_class) or doc_type in ("other", "unknown"):
        return None      # closed set; "other" teaches nothing
    text = _page1_text_from_file(str(meta.get("path", "")))
    emb = (mc.embed([text[:8000]]) or [[]])[0] if text.strip() else []
    row = {"ts": runstore.now_iso(), "run_id": str(run_id)[:32],
           "doc_class": doc_class, "doc_type": doc_type,
           "markers": _markers_sig(stage_a),
           "emb": emb, "emb_kind": "text", "dims": len(emb),
           "source": source}
    out = [row]
    if with_vision:
        v = _vision_descriptor_from_file(str(meta.get("path", "")))
        if v:
            vemb = (mc.embed([v]) or [[]])[0]
            if vemb:
                out.append({**row, "emb": vemb, "emb_kind": "vision",
                            "dims": len(vemb)})
    with _LOCK:
        config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return row


def _vision_descriptor_from_file(path: str) -> str:
    """Render page 1 and ask the vision model for a short layout description
    (study-job only — the hot path uses the already-rendered raw.images)."""
    try:
        import tempfile

        import fitz
        with fitz.open(path) as doc:
            if not doc.page_count:
                return ""
            pix = doc[0].get_pixmap(dpi=120)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                pix.save(tmp.name)
                return mc.vision("VISION", DESCRIBE_PROMPT, [tmp.name]).strip()[:400]
    except Exception:
        return ""


def drop(run_id: str, source: str = "") -> int:
    """Remove a document's profile rows (undo, F1): all sources, or one."""
    p = _path()
    if not p.exists():
        return 0
    keep, dropped = [], 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            keep.append(line)
            continue
        if row.get("run_id") == run_id and (not source or row.get("source") == source):
            dropped += 1
        else:
            keep.append(line)
    if dropped:
        with _LOCK:
            config.atomic_write_text(p, ("\n".join(keep) + "\n") if keep else "")
    return dropped


# ---------------------------------------------------------------- matching ----
class Vote:
    def __init__(self, doc_type: str = "", why: str = "", decisive: bool = False):
        self.doc_type, self.why, self.decisive = doc_type, why, decisive


def markers_vote(rows: list[dict], raw) -> Vote:
    """Zero-model tier: profiles whose page-marker signature EQUALS the live
    document's. Decisive only when >= MARKERS_MIN_DOCS distinct documents agree
    unanimously and no same-signature profile backs any other type."""
    sig = _raw_sig(raw)
    by_type: dict[str, set] = {}
    for r in rows:
        if r.get("markers") == sig:
            by_type.setdefault(str(r.get("doc_type")), set()).add(r.get("run_id"))
    if len(by_type) != 1:
        return Vote()
    (t, docs), = by_type.items()
    if len(docs) >= MARKERS_MIN_DOCS:
        return Vote(t, f"marker signature matches {len(docs)} taught document(s)",
                    True)
    return Vote()


def _best_by_type(rows: list[dict], vec: list[float], kind: str) -> dict:
    best: dict[str, dict] = {}
    for r in rows:
        if r.get("emb_kind") != kind or not r.get("emb"):
            continue
        sim = _cos(vec, r["emb"])
        t = str(r.get("doc_type"))
        b = best.setdefault(t, {"sim": 0.0, "docs": set()})
        b["docs"].add(r.get("run_id"))
        if sim > b["sim"]:
            b["sim"] = sim
    return best


def _pick(best: dict, kind: str) -> tuple[str, str]:
    if not best:
        return "", ""
    ranked = sorted(best.items(), key=lambda kv: kv[1]["sim"], reverse=True)
    top_t, top = ranked[0]
    second = ranked[1][1]["sim"] if len(ranked) > 1 else 0.0
    if (top["sim"] >= MIN_SIM and top["sim"] - second >= MIN_MARGIN
            and len(top["docs"]) >= MIN_SUPPORT):
        return top_t, (f"{kind} similarity {top['sim']:.2f} to "
                       f"{len(top['docs'])} taught document(s)")
    return "", ""


def embed_match(rows: list[dict], raw) -> tuple[str, str]:
    """ONE embed call over page-1 text; cosine against text-kind prototypes.
    Cache key includes the text hash — the ladder re-runs extract and must
    re-embed only when its second pass enriched page 1."""
    page1 = (getattr(raw, "page_texts", {}).get(0)
             or getattr(raw, "survey_texts", {}).get(0)
             or getattr(raw, "raw_text", ""))[:8000]
    if not page1.strip():
        return "", ""
    key = f"{getattr(raw, 'sha256', '')}:{hashlib.sha1(page1.encode()).hexdigest()[:12]}"
    vec = _embed_cached(key, page1)
    if not vec:
        return "", ""
    return _pick(_best_by_type(rows, vec, "text"), "text")


def vision_match(rows: list[dict], raw) -> tuple[str, str]:
    """ONE short vision-descriptor call (effort >= 4, scans) matched against
    vision-kind prototypes. Skips silently without an image or prototypes."""
    if not any(r.get("emb_kind") == "vision" and r.get("emb") for r in rows):
        return "", ""
    images = getattr(raw, "images", None) or []
    if not images:
        return "", ""
    desc = ""
    try:
        desc = mc.vision("VISION", DESCRIBE_PROMPT, [images[0]]).strip()[:400]
    except Exception:
        return "", ""
    if not desc:
        return "", ""
    vec = (mc.embed([desc]) or [[]])[0]
    if not vec:
        return "", ""
    return _pick(_best_by_type(rows, vec, "vision"), "vision")


# ---------------------------------------------------------------- the prior ---
def apply_prior(ext, raw, types, *, weak_fallback: bool, engine: str,
                quality: bool) -> None:
    """The doc-type prior (F5). Fills ONLY the weak `type_hint or other`
    fallback; a valid model/strong type is never overridden — at most flagged
    uncertain. Deterministic overrides, packet guards and eval stay in charge."""
    from . import config as _config, runctl
    if not runctl.override("doctype_prior", True):
        return                              # eval / kill switch
    prov = (getattr(ext, "provenance", {}) or {}).get("doc_type") or {}
    if prov.get("source") == "rule":
        return                              # overrides + packet guards won
    fenced = (getattr(raw, "editable", False)
              or getattr(raw, "ext", "") in _config.EMAIL_EXTS
              or (ext.doc_class == "bank" and getattr(raw, "invoice_pages", None)
                  and not getattr(raw, "bank_letter_pages", None))
              or getattr(raw, "type_hint", "") == "invoice")
    if fenced:
        return
    try:
        rows = load_for(ext.doc_class)
    except Exception:
        return
    if not rows:
        return                              # empty memory = total no-op
    vote = markers_vote(rows, raw)
    if weak_fallback:
        cand, why = ("", "")
        if vote.decisive:
            cand, why = vote.doc_type, vote.why
        elif engine != "deterministic":
            cand, why = embed_match(rows, raw)
            if not cand and quality:
                cand, why = vision_match(rows, raw)
        if (cand and cand in types and cand != ext.doc_type
                and not (ext.doc_class == "bank" and cand == "invoice"
                         and getattr(raw, "bank_letter_pages", None))):
            ext.warnings.append(
                f"doc-type prior: {why} — '{ext.doc_type}' -> '{cand}' (pattern)")
            ext.doc_type = cand
            ext.provenance["doc_type"] = {"source": "pattern", "page": None}
    elif vote.decisive and vote.doc_type != ext.doc_type:
        # the memory disagrees with a VALID model answer: flag, never override
        ext.doc_type_uncertain = True
        ext.warnings.append(
            f"doc-type prior disagrees ({vote.why} say {vote.doc_type}) — "
            "flagged uncertain")


# ---------------------------------------------------------------- study job ---
def study(log=print, cancel=None, on_progress=None, with_vision: bool = False) -> dict:
    """Walk the documents the operator already fed the pipeline and build
    profiles for every one whose type is TRUSTED: a label's doc_type_gold, or
    a 👍-endorsed run's reported type. Everything else is skipped — the memory
    must not learn the machine's own unconfirmed guesses. Cancelable."""
    from . import ratings
    from .dataset import load_labels
    labels = {l.get("doc_sha256"): l for l in load_labels()}
    ups = {rid for rid, r in ratings.latest().items() if r == "up"}
    runs = [r for r in runstore.list_runs() if not r.get("test")]
    added, skipped, by_type = 0, 0, {}
    for i, r in enumerate(runs):
        if cancel is not None and cancel.is_set():
            log(f"canceled after {i} of {len(runs)} document(s)")
            break
        if on_progress:
            on_progress(f"profiling {i + 1}/{len(runs)}",
                        int(100 * (i + 1) / max(1, len(runs))))
        rid = r["run_id"]
        lab = labels.get(rid)
        doc_type = (lab or {}).get("doc_type_gold", "")
        if not doc_type and rid not in ups:
            skipped += 1
            continue                        # unconfirmed machine guess — skip
        row = capture(rid, "study", doc_type=doc_type, with_vision=with_vision)
        if row:
            added += 1
            by_type[row["doc_type"]] = by_type.get(row["doc_type"], 0) + 1
            log(f"  {r['file']}: {row['doc_type']}"
                + (" (+vision)" if with_vision else ""))
        else:
            skipped += 1
    report = {"ts": runstore.now_iso(), "documents": len(runs), "added": added,
              "skipped": skipped, "by_type": by_type,
              "profiles_total": len(load())}
    config.EVAL_DIR.mkdir(parents=True, exist_ok=True)
    config.atomic_write_text(config.EVAL_DIR / "pattern_study.json",
                             json.dumps(report, ensure_ascii=False, indent=1))
    log(f"study done: {added} profile(s) added, {skipped} skipped, "
        f"{report['profiles_total']} total")
    return report
