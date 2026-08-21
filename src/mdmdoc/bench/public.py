"""Public handwriting / form samples for the benchmark (no registration needed).

  FUNSD          scanned forms (typed + handwritten fields), EN — 16 MB zip
  school_notebooks_RU / _EN (ai-forever, MIT) — photos of handwritten school
                 notebooks; images.zip is 2.9 GB / 356 MB, so single pages are
                 pulled with HTTP range requests through a file-like wrapper.

Everything lands in bench/public/<source>/ and is added to the manifest with
stratum "public"; gold still comes from Claude like for any other document.
"""
from __future__ import annotations

import io
import json
import random
import sys
import zipfile
from pathlib import Path

import requests

from .. import config
from . import manifest

FUNSD_URL = "https://guillaumejaume.github.io/FUNSD/dataset.zip"
NOTEBOOKS = {
    "RU": "https://huggingface.co/datasets/ai-forever/school_notebooks_RU/resolve/main/images.zip",
    "EN": "https://huggingface.co/datasets/ai-forever/school_notebooks_EN/resolve/main/images.zip",
}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def public_dir() -> Path:
    return config.BENCH_DIR / "public"


class HttpFile(io.RawIOBase):
    """Seekable read-only view of a remote file via HTTP Range requests."""

    def __init__(self, url: str):
        self.url = url
        self.sess = requests.Session()
        r = self.sess.head(url, allow_redirects=True, timeout=60)
        r.raise_for_status()
        self.size = int(r.headers["Content-Length"])
        self.url = r.url
        self.pos = 0
        self.requests = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = off
        elif whence == io.SEEK_CUR:
            self.pos += off
        else:
            self.pos = self.size + off
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n == 0 or self.pos >= self.size:
            return b""
        end = min(self.size, self.pos + n) - 1
        r = self.sess.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=120)
        r.raise_for_status()
        self.requests += 1
        data = r.content
        self.pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def fetch_funsd(n: int = 8, seed: int = 7) -> list[Path]:
    out = public_dir() / "funsd"
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / "dataset.zip"
    if not zpath.exists():
        _log(f"downloading FUNSD ({FUNSD_URL})")
        r = requests.get(FUNSD_URL, timeout=600)
        r.raise_for_status()
        zpath.write_bytes(r.content)
    picked: list[Path] = []
    with zipfile.ZipFile(zpath) as z:
        names = sorted(m for m in z.namelist() if "/testing_data/images/" in m and m.endswith(".png")
                       and "__MACOSX" not in m and not Path(m).name.startswith("._"))
        rnd = random.Random(seed)
        rnd.shuffle(names)
        for name in names[:n]:
            target = out / Path(name).name
            if not target.exists():
                target.write_bytes(z.read(name))
            picked.append(target)
    return picked


def fetch_notebooks(lang: str, n: int = 6, seed: int = 7) -> list[Path]:
    url = NOTEBOOKS[lang.upper()]
    out = public_dir() / f"notebooks_{lang.lower()}"
    out.mkdir(parents=True, exist_ok=True)
    existing = sorted(p for p in out.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if len(existing) >= n:
        return existing[:n]
    hf = HttpFile(url)
    _log(f"remote zip {url.split('/')[-3]}: {hf.size / 1e6:.0f} MB — reading the central directory")
    picked: list[Path] = []
    with zipfile.ZipFile(hf) as z:
        names = sorted(m for m in z.namelist()
                       if m.lower().endswith((".jpg", ".jpeg", ".png"))
                       and "__MACOSX" not in m and not Path(m).name.startswith("._"))
        rnd = random.Random(seed)
        rnd.shuffle(names)
        for name in names[:n]:
            target = out / Path(name).name
            if not target.exists():
                target.write_bytes(z.read(name))
                _log(f"  pulled {name} ({target.stat().st_size / 1e6:.1f} MB)")
            picked.append(target)
    _log(f"  {hf.requests} range request(s)")
    return picked


def cli_public(a) -> int:
    added = 0
    if a.funsd:
        files = fetch_funsd(a.funsd)
        manifest.add(files, kind="scan", langs=["en"], tags=["public", "form", "funsd"],
                     expected_doc_type="scanned form (FUNSD)", stratum="public")
        added += len(files)
    for lang, n in (("EN", a.notebooks_en), ("RU", a.notebooks_ru)):
        if n:
            files = fetch_notebooks(lang, n)
            manifest.add(files, kind="photo", langs=[lang.lower()],
                         tags=["public", "handwriting", "notebook", f"notebooks_{lang.lower()}"],
                         expected_doc_type=f"handwritten school notebook page ({lang})", stratum="public")
            added += len(files)
    _log(f"public stratum: {added} file(s) added/updated")
    return 0


def tag_handwriting_from_gold() -> int:
    """Add the `handwriting` tag to documents whose gold says handwriting_present."""
    from .gold import gold_path
    docs = manifest.load_all()
    n = 0
    for d in docs:
        for p in d.pages:
            gp = gold_path(d.doc_id, p)
            if gp.exists():
                try:
                    f = json.loads(gp.read_text(encoding="utf-8")).get("final") or {}
                except Exception:
                    continue
                if f.get("handwriting_present") and "handwriting" not in d.tags:
                    d.tags = sorted(set(d.tags + ["handwriting"]))
                    n += 1
                    break
    manifest.save_all(docs)
    return n
