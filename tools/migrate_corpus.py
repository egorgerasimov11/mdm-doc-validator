#!/usr/bin/env python3
"""
migrate_corpus.py — make the labeled corpus portable.

Legacy labels.jsonl rows point at ABSOLUTE MacBook paths (Desktop/Downloads/…),
so an eval on any other machine dies with 'missing file'. This one-shot tool:

  1. copies every labeled original into  dataset/corpus/<doc_class>/<basename>
     (collision → sha8 prefix; already-copied files are reused, so it is
     idempotent);
  2. rewrites each row's doc_path to the CORPUS-RELATIVE posix path;
  3. backs labels.jsonl up to labels.jsonl.bak-<UTC-stamp> first.

After it runs, `rsync dataset/corpus dataset/labels.jsonl` to another machine
is all an eval needs (dataset.resolve_doc_path handles both forms forever).

dataset/corpus/ is GITIGNORED — the originals carry real PII and must never
reach the repo. Run:  uv run python tools/migrate_corpus.py [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

PY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PY_ROOT / "src"))

from mdmdoc import config  # noqa: E402
from mdmdoc.dataset import load_labels, resolve_doc_path  # noqa: E402


def _sha8(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def migrate(dry_run: bool = False) -> int:
    labels = load_labels()
    if not labels:
        print("no labels — nothing to migrate")
        return 0
    corpus = config.CORPUS_DIR
    changed = 0
    missing = 0
    out_rows: list[dict] = []
    for lab in labels:
        raw = str(lab.get("doc_path") or "")
        if raw and not Path(raw).is_absolute():
            out_rows.append(lab)          # already portable
            continue
        src = resolve_doc_path(raw)
        if not src.exists():
            print(f"  ! missing original, row kept as-is: {raw}")
            missing += 1
            out_rows.append(lab)
            continue
        sub = corpus / str(lab.get("doc_class") or "misc")
        dst = sub / src.name
        if dst.exists() and _sha8(dst) != _sha8(src):
            dst = sub / f"{_sha8(src)}-{src.name}"
        rel = dst.relative_to(corpus).as_posix()
        print(f"  {raw}\n    -> {rel}")
        if not dry_run:
            sub.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
            lab = {**lab, "doc_path": rel}
        changed += 1
        out_rows.append(lab)

    if dry_run:
        print(f"dry run: {changed} row(s) would be rewritten, {missing} missing")
        return 0
    if changed:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = config.LABELS_PATH.with_name(f"labels.jsonl.bak-{stamp}")
        shutil.copy2(config.LABELS_PATH, backup)
        config.LABELS_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in out_rows) + "\n",
            encoding="utf-8")
        print(f"rewrote {changed} row(s); backup at {backup.name}")
    else:
        print("nothing to rewrite — corpus already portable")
    if missing:
        print(f"WARNING: {missing} original(s) not found — fix by hand")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(migrate(dry_run="--dry-run" in sys.argv))
