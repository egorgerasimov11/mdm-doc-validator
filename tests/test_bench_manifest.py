"""Corpus manifest + render cache (offline; tesseract optional)."""
import json
from pathlib import Path

import fitz
import pytest
from PIL import Image

from mdmdoc import config
from mdmdoc.bench import manifest
from mdmdoc.extract import render


@pytest.fixture()
def bench_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BENCH_DIR", tmp_path / "bench")
    return tmp_path / "bench"


def _make_pdf(path: Path, pages: list[str]) -> Path:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page(width=595, height=842)
        y = 72
        for line in text.split("\n"):
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    doc.save(path)
    doc.close()
    return path


def _make_image(path: Path) -> Path:
    im = Image.new("RGB", (900, 1200), "white")
    im.save(path)
    return path


def test_sniff_digital_pdf(bench_dir, tmp_path):
    pdf = _make_pdf(tmp_path / "letter.pdf", [
        "Bank of America confirms that Acme LLC holds account 4830 2291 0077.\n"
        "SWIFT BOFAUS3N. Routing 026009593. Sincerely, Jane Doe."] * 2)
    info = manifest.sniff(pdf)
    assert info["kind"] == "digital"
    assert info["pages_total"] == 2
    assert "text_layer" in info["tags"] and "multipage" in info["tags"]
    assert "Latin" in info["scripts"] and "en" in info["langs"]


def test_add_updates_same_sha_and_filters(bench_dir, tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", ["hello world this is a digital page with text"])
    img = _make_image(tmp_path / "photo.jpg")
    added = manifest.add([pdf, img], tags=["core"])
    assert len(added) == 2
    rows = manifest.load("all")
    assert len(rows) == 2
    # re-adding the same file updates in place (manual fields win)
    manifest.add([pdf], langs=["de"], expected_doc_type="bank letter", tags=["seal"])
    rows = manifest.load("all")
    assert len(rows) == 2
    a = next(d for d in rows if d.name == "a.pdf")
    assert a.langs == ["de"] and a.expected_doc_type == "bank letter"
    assert {"core", "seal", "text_layer"} <= set(a.tags)
    # filters
    assert len(manifest.load("tag:core")) == 2
    assert len(manifest.load("kind:photo")) == 1
    assert len(manifest.load("lang:de")) == 1
    assert len(manifest.load("tag:core&kind:photo")) == 1
    assert len(manifest.load("kind:photo,kind:digital")) == 2
    assert len(manifest.load("not:kind:photo")) == 1
    assert manifest.get(a.doc_id[:6]).doc_id == a.doc_id
    with pytest.raises(ValueError):
        manifest.load("bogus:x")
    # persisted as jsonl with the expected keys
    line = json.loads(manifest.manifest_path().read_text().splitlines()[0])
    assert {"doc_id", "sha256", "path", "kind", "pages", "tags", "stratum"} <= set(line)


def test_add_directory_and_container(bench_dir, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    _make_pdf(d / "x.pdf", ["some text on a page for the manifest test"])
    _make_image(d / "y.png")
    (d / "notes.txt").write_text("ignored")
    # a zip container expands into bench/extracted/<sha>/
    import zipfile
    z = tmp_path / "packet.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(d / "x.pdf", "inner/x.pdf")
    added = manifest.add([d, z])
    names = sorted(a.name for a in added)
    assert names == ["x.pdf", "x.pdf", "y.png"]
    # same bytes → same sha → one manifest row for x.pdf (container wins last)
    rows = manifest.load("all")
    assert len(rows) == 2
    x = next(r for r in rows if r.name == "x.pdf")
    assert x.source_container and x.source_container.endswith("packet.zip")


def test_render_cache_and_tiles(bench_dir, tmp_path, monkeypatch):
    # avoid tesseract in this test: rotation detection is exercised separately
    from mdmdoc import ocr
    monkeypatch.setattr(ocr, "HAVE_TESSERACT", False)
    pdf = _make_pdf(tmp_path / "r.pdf", ["render me"])
    cache = bench_dir / "render" / "x"
    p1 = render.render_page(pdf, cache, 0, render.PRESETS["v200"])
    assert p1.suffix == ".jpg" and p1.exists()
    m1 = p1.stat().st_mtime_ns
    p2 = render.render_page(pdf, cache, 0, render.PRESETS["v200"])
    assert p2 == p1 and p2.stat().st_mtime_ns == m1          # cache hit
    w, h = Image.open(p1).size
    assert max(w, h) <= 2000
    g = render.render_page(pdf, cache, 0, render.PRESETS["g300"])
    assert g.suffix == ".png" and Image.open(g).mode == "L"
    tiles = render.tiles(p1, "q4")
    assert len(tiles) == 4 and all(t.exists() for t in tiles)
    assert all(max(Image.open(t).size) >= 1100 for t in tiles)
    meta = json.loads((cache / "meta.json").read_text())
    assert meta["rotation"]["0"] == 0
    # manual override is honoured by the cache name
    render.set_page_rotation(cache, 0, 90)
    p3 = render.render_page(pdf, cache, 0, render.PRESETS["v200"])
    assert p3.name.endswith("_r90.jpg")
    assert render.prune(cache, "v200") >= 2


def test_image_source_render(bench_dir, tmp_path, monkeypatch):
    from mdmdoc import ocr
    monkeypatch.setattr(ocr, "HAVE_TESSERACT", False)
    img = _make_image(tmp_path / "p.jpg")
    out = render.render_page(img, bench_dir / "render" / "i", 0, render.PRESETS["v170"])
    assert max(Image.open(out).size) <= 1600
    assert render.page_count(img) == 1


def test_synthetic_stratum(bench_dir):
    docs_dir = config.EVAL_DIR / "synthetic" / "docs"
    if not docs_dir.exists():
        pytest.skip("synthetic corpus not present")
    n = manifest.build_synthetic()
    assert n >= 50
    rows = manifest.load("stratum:synthetic")
    assert len(rows) == n
    assert all(r.gold_source == "textlayer" for r in rows)
    assert any("w9" in r.tags for r in rows)
    assert manifest.text_layer(rows[0], 0).strip()
