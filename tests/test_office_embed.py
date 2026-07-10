"""D7: Office containers — workbook OLE embeddings carve to real documents,
.msg attachments parse via MAPI streams, form-without-documents errs clearly."""
import zipfile

import fitz
import pytest

from mdmdoc import config, office_embed
from mdmdoc.pipeline import UnreadableDocument, run_check


def _pdf_bytes(text="Bank confirmation letter. This letter is to confirm the "
                    "account details below. IBAN DE89 3704 0044 0532 0130 00. "
                    "Account holder: Fake Corp GmbH") -> bytes:
    d = fitz.open()
    pg = d.new_page()
    y = 80
    for line in text.split(". "):
        pg.insert_text((72, y), line, fontsize=10)
        y += 16
    # bulk the file past the keep threshold (real support docs are never tiny)
    for i in range(3):
        extra = d.new_page()
        for j in range(40):
            extra.insert_text((72, 60 + j * 18), f"terms and conditions line {i}-{j}",
                              fontsize=10)
    out = d.tobytes()
    d.close()
    assert len(out) >= 2000
    return out


def _workbook(path, embed: list[bytes] | None = None):
    """Minimal OOXML zip that LOOKS like a workbook; oleObject bins carry the
    given payloads raw (the carve fallback finds %PDF- without a real OLE)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
        for i, data in enumerate(embed or [], start=1):
            z.writestr(f"xl/embeddings/oleObject{i}.bin",
                       b"\x00" * 64 + data + b"\x00" * 16)
        z.writestr("xl/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    return path


def test_magic_bytes_win_over_labels():
    assert office_embed.extension_from_bytes(b"%PDF-1.7 x" + b"y" * 10) == ".pdf"
    assert office_embed.extension_from_bytes(b"PK\x03\x04" + b"xl/x" * 10) == ".xlsx"
    assert office_embed.extension_from_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") == ".ole"


def test_carve_pdf_from_ole_stream():
    blob = b"junkjunk" + b"%PDF-1.4 body %%EOF" + b"tail"
    carved = office_embed.carve_known_payloads(blob)
    assert carved and carved[0].startswith(b"%PDF-") and carved[0].endswith(b"%%EOF")


def test_workbook_embeddings_extracted(tmp_path):
    wb = _workbook(tmp_path / "req.xlsm", embed=[_pdf_bytes()])
    assert office_embed.workbook_has_embeddings(wb) is True
    saved = office_embed.extract_workbook_embeddings(wb, tmp_path / "out")
    assert len(saved) == 1 and saved[0].suffix == ".pdf"
    assert saved[0].read_bytes().startswith(b"%PDF-")


def test_media_decoration_ignored(tmp_path):
    wb = _workbook(tmp_path / "plain.xlsm", embed=[])
    assert office_embed.workbook_has_embeddings(wb) is False


def test_workbook_container_run_analyses_embedded_letter(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    wb = _workbook(tmp_path / "request.xlsm", embed=[_pdf_bytes()])
    res = run_check(wb, "bank", use_vision=False, engine="deterministic",
                    enforce_approvals=False)
    assert res.pub["doc_type"] == "bank_letter"        # NOT editable_source/BNK-003
    assert any("container" in w or "analysed" in w for w in res.pub["warnings"])


def test_workbook_without_embeddings_still_editable_reject(tmp_path, monkeypatch):
    """No embedded docs -> not a container -> today's BNK-003 editable path."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    wb = _workbook(tmp_path / "form_only.xlsm", embed=[])
    res = run_check(wb, "bank", use_vision=False, engine="deterministic",
                    enforce_approvals=False)
    assert res.pub["doc_type"] == "editable_source"
    assert any(f.rule_id == "BNK-003" for f in res.findings)


def test_msg_attachments_via_fake_ole(tmp_path, monkeypatch):
    """olefile cannot write CFB, so the MAPI reader is tested through a fake
    OleFileIO exposing the same listdir/openstream surface."""
    pdf = _pdf_bytes()

    class FakeOle:
        def __init__(self, *a, **k):
            pass

        def listdir(self, streams=True, storages=False):
            return [["__attach_version1.0_#00000000", "__substg1.0_37010102"],
                    ["__attach_version1.0_#00000000", "__substg1.0_3707001F"],
                    ["__attach_version1.0_#00000001", "__substg1.0_37010102"],
                    ["__attach_version1.0_#00000001", "__substg1.0_3707001F"]]

        def openstream(self, stream):
            import io
            slot, leaf = stream[0], stream[-1]
            if leaf.startswith("__substg1.0_37010102"):
                data = pdf if slot.endswith("#00000000") else b"tiny"
                return io.BytesIO(data)
            name = ("bank letter.pdf" if slot.endswith("#00000000")
                    else "logo.png").encode("utf-16-le")
            return io.BytesIO(name)

        def close(self):
            pass

    monkeypatch.setattr(office_embed.olefile, "isOleFile", lambda p: True)
    monkeypatch.setattr(office_embed.olefile, "OleFileIO", FakeOle)
    saved = office_embed.extract_msg_attachments(tmp_path / "mail.msg",
                                                 tmp_path / "out")
    assert len(saved) == 1                          # tiny non-doc dropped
    assert saved[0].name == "bank letter.pdf"
    assert saved[0].read_bytes().startswith(b"%PDF-")
