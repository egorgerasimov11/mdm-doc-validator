#!/usr/bin/env python3
"""
gen_abap_golden.py — generate the ABAP golden data class from golden_cases.json.

The same corpus that run_golden.py drives through the Python deterministic engine
becomes a generated ABAP data class + a loop testclass, so the ABAP twin must
reproduce identical fields/notes (behavioural parity, verified by ABAP Unit
on-system). doc_type is BAKED per case (ABAP build() takes it as input — there is
no ABAP type_hint port), captured from the Python deterministic run so the two
sides start from the same doc_type.

Emits into the ABAP repo (MDMDOC_ABAP_HOME or the sibling checkout):
  src/zcl_mdmdoc_golden_data.clas.abap   (data, "DO NOT EDIT", GOLDEN-HASH header)
  src/zcl_mdmdoc_golden_data.clas.xml

Run:  uv run python tools/golden/gen_abap_golden.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_golden  # noqa: E402

ABAP_ROOT = Path(os.environ.get("MDMDOC_ABAP_HOME",
                                str(Path.home() / "Projects" / "mdm-doc-validator-abap")))


def _lit(s: str) -> str:
    """A backtick ABAP string literal, chunked to keep lines <=140 chars
    (abaplint line_length). Backticks in the text are doubled."""
    s = s.replace("`", "``")
    if len(s) <= 100:
        return f"`{s}`"
    parts, i = [], 0
    while i < len(s):
        parts.append(s[i:i + 90])
        i += 90
    return " &&\n          ".join(f"`{p}`" for p in parts)


def _case_abap(c: dict, doc_type: str) -> str:
    exp = c.get("expect", {})
    lines = [
        "    APPEND VALUE #(",
        f"      id = `{c['id']}`",
        f"      doc_class = `{c['doc_class']}`",
        f"      doc_type = `{doc_type}`",
        f"      filename = `{c.get('filename', c['id'] + '.pdf')}`",
        f"      raw_text = {_lit(c['raw_text'])}",
    ]
    fields = exp.get("fields") or {}
    if fields:
        lines.append("      exp_fields = VALUE #(")
        for k, v in fields.items():
            val = "true" if v is True else "false" if v is False else str(v)
            lines.append(f"        ( name = `{k}` value = `{val}` )")
        lines.append("      )")
    notes = exp.get("notes") or []
    if notes:
        lines.append("      exp_notes = VALUE #(")
        for n in notes:
            lines.append(f"        ( {_lit(n)} )")
        lines.append("      )")
    lines.append("    ) TO gt_cases.")
    return "\n".join(lines)


def build_abap(cases_with_types: list[tuple[dict, str]], corpus_hash: str) -> str:
    body = "\n".join(_case_abap(c, dt) for c, dt in cases_with_types)
    return f"""\" GENERATED from tools/golden/golden_cases.json by tools/golden/gen_abap_golden.py
\" *** DO NOT EDIT BY HAND — edit the JSON corpus and re-run the generator ***
\" GOLDEN-HASH {corpus_hash}
CLASS zcl_mdmdoc_golden_data DEFINITION
  PUBLIC
  FINAL
  CREATE PRIVATE.

  PUBLIC SECTION.
    TYPES: BEGIN OF ty_exp_field,
             name  TYPE string,
             value TYPE string,
           END OF ty_exp_field,
           tt_exp_fields TYPE STANDARD TABLE OF ty_exp_field WITH DEFAULT KEY.
    TYPES: BEGIN OF ty_case,
             id         TYPE string,
             doc_class  TYPE string,
             doc_type   TYPE string,
             filename   TYPE string,
             raw_text   TYPE string,
             exp_fields TYPE tt_exp_fields,
             exp_notes  TYPE string_table,
           END OF ty_case,
           tt_cases TYPE STANDARD TABLE OF ty_case WITH DEFAULT KEY.

    CLASS-DATA gt_cases TYPE tt_cases READ-ONLY.
    CLASS-METHODS class_constructor.

    \" corpus hash of the JSON this was generated from (check_parity freshness)
    CONSTANTS c_golden_hash TYPE string VALUE `{corpus_hash}`.
ENDCLASS.


CLASS zcl_mdmdoc_golden_data IMPLEMENTATION.

  METHOD class_constructor.
{body}
  ENDMETHOD.

ENDCLASS.
"""


CLAS_XML = """<?xml version="1.0" encoding="utf-8"?>
<abapGit version="v1.0.0" serializer="LCL_OBJECT_CLAS" serializer_version="v1.0.0">
 <asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">
  <asx:values>
   <VSEOCLASS>
    <CLSNAME>ZCL_MDMDOC_GOLDEN_DATA</CLSNAME>
    <LANGU>E</LANGU>
    <DESCRIPT>mdmdoc: GENERATED golden parity corpus (do not edit)</DESCRIPT>
    <STATE>1</STATE>
    <CLSCCINCL>X</CLSCCINCL>
    <FIXPT>X</FIXPT>
    <UNICODE>X</UNICODE>
   </VSEOCLASS>
  </asx:values>
 </asx:abap>
</abapGit>
"""


def main() -> int:
    cwt = []
    for c in run_golden.load_cases():
        raw = run_golden._build_raw(c)
        from mdmdoc.stage_b import extract
        ext = extract(raw, engine="deterministic")
        # refuse to emit a stale corpus: the Python case must still pass
        res = run_golden.run_case(c)
        if not res["ok"]:
            print(f"REFUSING: Python case {c['id']} fails: {res['fails']}")
            return 1
        cwt.append((c, ext.doc_type or "other"))
    h = run_golden.corpus_hash()
    src = ABAP_ROOT / "src"
    (src / "zcl_mdmdoc_golden_data.clas.abap").write_text(build_abap(cwt, h))
    (src / "zcl_mdmdoc_golden_data.clas.xml").write_text(CLAS_XML)
    print(f"generated zcl_mdmdoc_golden_data ({len(cwt)} cases, GOLDEN-HASH {h}) into {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
