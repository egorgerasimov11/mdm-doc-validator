"""Corpus portability: labels store CORPUS-relative doc_path; resolution keeps
legacy absolute rows working and falls back to corpus/<basename> on another
machine (the mini eval used to die with 16x 'missing file')."""
from pathlib import Path

from mdmdoc import config
from mdmdoc.dataset import portable_doc_path, resolve_doc_path


def test_relative_resolves_under_corpus():
    assert resolve_doc_path("bank/letter.pdf") == config.CORPUS_DIR / "bank/letter.pdf"


def test_absolute_existing_is_honored(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"x")
    assert resolve_doc_path(str(p)) == p


def test_absolute_missing_falls_back_to_corpus_basename():
    ghost = "/Users/nobody/Desktop/documents/banking/eu/letter.pdf"
    assert resolve_doc_path(ghost) == config.CORPUS_DIR / "letter.pdf"


def test_portable_path_relative_under_corpus(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path)
    doc = tmp_path / "bank" / "a.pdf"
    doc.parent.mkdir()
    doc.write_bytes(b"x")
    assert portable_doc_path(str(doc)) == "bank/a.pdf"


def test_portable_path_outside_corpus_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "corpus")
    assert portable_doc_path("/somewhere/else/b.pdf") == "/somewhere/else/b.pdf"
    assert portable_doc_path("") == ""
