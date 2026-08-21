#!/usr/bin/env python3
"""
gen_prompts_abap.py — carry the vision prompts from the Python repo into the ABAP twin.

The two validators must ask the model the SAME thing. They did not: ABAP's
`ZCL_MDMDOC_LLM=>system_vision` was three hand-written sentences, while the Python
prompt states nine rules the benchmark showed to matter — above all "never translate
or romanize", without which a CJK page comes back in latin letters and every value on
it is lost.

A prompt is DATA, not logic, so it belongs with the things that auto-sync (rule data,
golden corpora) rather than with hand-ported predicates. This generator is the sync.

Emits into the ABAP repo (MDMDOC_ABAP_HOME or the sibling checkout):
  src/zcl_mdmdoc_prompts.clas.abap   ("DO NOT EDIT", GEN-HASH header)
  src/zcl_mdmdoc_prompts.clas.xml

Run:    uv run python tools/gen_prompts_abap.py
Check:  uv run python tools/gen_prompts_abap.py --check   (exit 1 if stale)
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ABAP_ROOT = Path(os.environ.get("MDMDOC_ABAP_HOME",
                                str(Path.home() / "Projects" / "mdm-doc-validator-abap")))

# (ABAP method, prompt file, one-line purpose)
PROMPTS: list[tuple[str, str, str]] = [
    ("vision_transcribe", "prompts/vision/transcribe_md.v1.txt",
     "Full-page transcription: verbatim, original script, tables as Markdown."),
    ("vision_transcribe_cjk", "prompts/vision/transcribe_cjk.v1.txt",
     "CJK rescue: re-read a page the model came back from in latin letters."),
]


def _lit(s: str, indent: int = 6) -> str:
    """A backtick ABAP string literal, chunked to keep lines <= 140 chars
    (abaplint line_length). Backticks are doubled; newlines become
    cl_abap_char_utilities=>newline concatenations."""
    pad = " " * indent
    out: list[str] = []
    for si, seg in enumerate(s.split("\n")):
        if si:
            out.append("cl_abap_char_utilities=>newline")
        seg = seg.replace("`", "``")
        if not seg:
            continue
        i = 0
        while i < len(seg):
            out.append(f"`{seg[i:i + 60]}`")
            i += 60
    if not out:
        return "``"
    return (" &&\n" + pad).join(out)


def load() -> list[dict]:
    rows = []
    for method, rel, purpose in PROMPTS:
        text = (ROOT / rel).read_text(encoding="utf-8").strip()
        rows.append({"method": method, "source": rel, "purpose": purpose, "text": text})
    return rows


def gen_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['method']}\x00{r['text']}\x00".encode())
    return h.hexdigest()[:16]


HEADER = '''" GENERATED from the Python reference prompts by tools/gen_prompts_abap.py
" *** DO NOT EDIT BY HAND — edit the prompt file and re-run the generator ***
" GEN-HASH {h}
"
" One prompt text, two callers. The Python extractor and ZMDMDOC must ask the vision
" model the same thing; the rules here are not decoration — "never translate or
" romanize" is what keeps a Korean or Japanese page from coming back in latin letters
" with every value on it lost.
CLASS zcl_mdmdoc_prompts DEFINITION
  PUBLIC
  FINAL
  CREATE PRIVATE.

  PUBLIC SECTION.
    CONSTANTS c_gen_hash TYPE string VALUE `{h}`.
{decls}ENDCLASS.


CLASS zcl_mdmdoc_prompts IMPLEMENTATION.
{impls}ENDCLASS.
'''

CLAS_XML = """<?xml version="1.0" encoding="utf-8"?>
<abapGit version="v1.0.0" serializer="LCL_OBJECT_CLAS" serializer_version="v1.0.0">
 <asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">
  <asx:values>
   <VSEOCLASS>
    <CLSNAME>ZCL_MDMDOC_PROMPTS</CLSNAME>
    <LANGU>E</LANGU>
    <DESCRIPT>mdmdoc: GENERATED model prompts, shared with the Python reference</DESCRIPT>
    <STATE>1</STATE>
    <CLSCCINCL>X</CLSCCINCL>
    <FIXPT>X</FIXPT>
    <UNICODE>X</UNICODE>
   </VSEOCLASS>
  </asx:values>
 </asx:abap>
</abapGit>
"""


def build_abap(rows: list[dict]) -> str:
    decls, impls = [], []
    for r in rows:
        decls.append(f"    \"! {r['purpose']}\n"
                     f"    \"! Source of truth: {r['source']}\n"
                     f"    CLASS-METHODS {r['method']}\n"
                     f"      RETURNING VALUE(rv_text) TYPE string.\n")
        impls.append(f"  METHOD {r['method']}.\n"
                     f"    rv_text =\n      {_lit(r['text'])}.\n"
                     f"  ENDMETHOD.\n")
    return HEADER.format(h=gen_hash(rows), decls="".join(decls), impls="\n".join(impls))


def main() -> int:
    check = "--check" in sys.argv
    rows = load()
    src = ABAP_ROOT / "src"
    if not src.is_dir():
        print(f"ABAP checkout not found at {ABAP_ROOT} (set MDMDOC_ABAP_HOME)", file=sys.stderr)
        return 0 if check else 1
    abap = build_abap(rows)
    target = src / "zcl_mdmdoc_prompts.clas.abap"
    if check:
        if (target.read_text(encoding="utf-8") if target.exists() else "") != abap:
            print(f"STALE: {target} does not match the Python prompts — "
                  f"run `uv run python tools/gen_prompts_abap.py`", file=sys.stderr)
            return 1
        print(f"prompts up to date (GEN-HASH {gen_hash(rows)})")
        return 0
    target.write_text(abap, encoding="utf-8")
    (src / "zcl_mdmdoc_prompts.clas.xml").write_text(CLAS_XML, encoding="utf-8")
    longest = max(len(ln) for ln in abap.split("\n"))
    print(f"generated zcl_mdmdoc_prompts (GEN-HASH {gen_hash(rows)}, longest line {longest}) into {src}")
    for r in rows:
        print(f"  {r['method']:24s} <- {r['source']} ({len(r['text'])} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
