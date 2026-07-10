#!/usr/bin/env python3
"""
office_embed.py — extract REAL document attachments out of Office containers
(D7): the OLE objects embedded in SAP request workbooks (xl/embeddings/
oleObject*.bin) and the attachment streams of Outlook .msg files.

Carving core ported from the excel-essential-attachments skill script (magic
bytes over labels: a PDF renamed .png is still a PDF; decoration in xl/media
is never touched). olefile is a small pure-python dependency; extract_msg was
rejected — it drags message-body machinery we don't need (attachments only).
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import olefile

# document payloads worth analysing; images are kept above the same size
# threshold the eml path uses (inline logos are tiny)
_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".msg", ".eml"}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
_IMG_MIN_BYTES = 50_000
_DOC_MIN_BYTES = 2_000


def clean_name(value: str) -> str:
    value = re.sub(r'[\\/:*"<>|\x00-\x1f]+', "-", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value[:120] or "attachment"


def extension_from_bytes(data: bytes) -> str:
    """Magic-byte identification — trusted over any label/filename."""
    if data.startswith(b"%PDF-"):
        return ".pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"PK\x03\x04"):
        head = data[:20000]
        if b"word/" in head:
            return ".docx"
        if b"xl/" in head:
            return ".xlsx"
        return ".zip"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ".ole"
    return ".bin"


def carve_known_payloads(data: bytes) -> list[bytes]:
    """%PDF-…%%EOF and PK\\x03\\x04 tails inside an OLE stream."""
    payloads: list[bytes] = []
    pdf_start = data.find(b"%PDF-")
    if pdf_start >= 0:
        pdf_end = data.rfind(b"%%EOF")
        end = pdf_end + 5 if pdf_end >= 0 else len(data)
        payloads.append(data[pdf_start:end])
    zip_start = data.find(b"PK\x03\x04")
    if zip_start >= 0:
        payloads.append(data[zip_start:])
    return payloads


def filename_hint(data: bytes) -> str | None:
    m = re.search(rb"([A-Za-z0-9_ .()&+\-]{3,}\.(?:pdf|docx?|xlsx?|msg|eml|png|jpe?g))",
                  data[:40000], re.I)
    return m.group(1).decode("latin1", errors="ignore") if m else None


def iter_ole_payloads(data: bytes):
    """Yield (name_hint, payload) document candidates from one OLE blob."""
    emitted = False
    if olefile.isOleFile(io.BytesIO(data)):
        ole = olefile.OleFileIO(io.BytesIO(data))
        try:
            for stream in ole.listdir(streams=True, storages=False):
                try:
                    content = ole.openstream(stream).read()
                except Exception:
                    continue
                carved = carve_known_payloads(content)
                if carved:
                    for payload in carved:
                        emitted = True
                        yield "/".join(stream), payload
                elif stream[-1] in ("CONTENTS", "Package"):
                    emitted = True
                    yield "/".join(stream), content
        finally:
            ole.close()
    if not emitted:
        for payload in carve_known_payloads(data):
            emitted = True
            yield "carved", payload


def _kept(data: bytes) -> bool:
    ext = extension_from_bytes(data)
    if ext in _DOC_EXTS:
        return len(data) >= _DOC_MIN_BYTES
    if ext in _IMG_EXTS:
        return len(data) >= _IMG_MIN_BYTES
    return False


def workbook_has_embeddings(path: Path) -> bool:
    """Cheap container trigger: does this OOXML workbook carry OLE objects?"""
    try:
        with zipfile.ZipFile(path) as zf:
            return any(n.startswith("xl/embeddings/") and not n.endswith("/")
                       for n in zf.namelist())
    except Exception:
        return False


def extract_workbook_embeddings(xlsx: Path, out: Path) -> list[Path]:
    """xl/embeddings/* -> real document files under `out` (magic-byte named).
    xl/media (decoration) is never touched."""
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    with zipfile.ZipFile(xlsx) as zf:
        names = sorted(n for n in zf.namelist()
                       if n.startswith("xl/embeddings/") and not n.endswith("/"))
        for idx, name in enumerate(names, start=1):
            data = zf.read(name)
            for hint, payload in iter_ole_payloads(data):
                if not _kept(payload):
                    continue
                ext = extension_from_bytes(payload)
                base = filename_hint(payload) or f"embedded-{idx}-{clean_name(hint)}"
                fname = clean_name(Path(base).stem) + ext
                p = out / fname
                n = 2
                while p.exists():
                    p = out / f"{Path(fname).stem}-{n}{ext}"
                    n += 1
                p.write_bytes(payload)
                saved.append(p)
    return saved


# --- Outlook .msg (OLE/CFB) attachment streams ---------------------------------
_ATTACH_DATA = "37010102"      # PR_ATTACH_DATA_BIN
_ATTACH_NAME_TAGS = ("3707001F", "3707001E", "3704001F", "3704001E")


def extract_msg_attachments(msg: Path, out: Path) -> list[Path]:
    """Minimal MAPI reader: __attach_version1.0_#N storages -> attachment
    bytes + long filename. Embedded-message attachments (3701000D storages)
    are skipped. Keeps the eml path's rules: pdf/images, size thresholds."""
    if not olefile.isOleFile(str(msg)):
        return []
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    ole = olefile.OleFileIO(str(msg))
    try:
        attachments: dict[str, dict] = {}
        for stream in ole.listdir(streams=True, storages=False):
            if not stream[0].startswith("__attach_version1.0_#"):
                continue
            slot = attachments.setdefault(stream[0], {})
            leaf = stream[-1]
            if leaf.startswith("__substg1.0_" + _ATTACH_DATA):
                try:
                    slot["data"] = ole.openstream(stream).read()
                except Exception:
                    continue
            else:
                for tag in _ATTACH_NAME_TAGS:
                    if leaf.startswith("__substg1.0_" + tag):
                        try:
                            raw = ole.openstream(stream).read()
                        except Exception:
                            break
                        enc = "utf-16-le" if tag.endswith("F") else "latin1"
                        slot.setdefault("name", raw.decode(enc, errors="ignore")
                                        .strip("\x00"))
                        break
        for key in sorted(attachments):
            slot = attachments[key]
            data = slot.get("data")
            if not data:
                continue
            ext = extension_from_bytes(data)
            name = clean_name(Path(slot.get("name") or "attachment").stem)
            if ext == ".bin":     # trust the MAPI filename's extension as fallback
                hinted = Path(slot.get("name") or "").suffix.lower()
                ext = hinted if hinted in (_DOC_EXTS | _IMG_EXTS) else ".bin"
            if ext == ".bin" or not _kept_msg(data, ext):
                continue
            p = out / f"{name}{ext}"
            n = 2
            while p.exists():
                p = out / f"{name}-{n}{ext}"
                n += 1
            p.write_bytes(data)
            saved.append(p)
    finally:
        ole.close()
    return saved


def _kept_msg(data: bytes, ext: str) -> bool:
    if ext == ".pdf" or ext in _DOC_EXTS:
        return len(data) >= _DOC_MIN_BYTES
    if ext in _IMG_EXTS:
        return len(data) >= 10_000   # mirror the eml keep threshold
    return False
