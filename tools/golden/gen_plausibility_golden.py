#!/usr/bin/env python3
"""
gen_plausibility_golden.py — golden parity corpus for the TEXT-LAYER PLAUSIBILITY GATE.

The gate decides whether a PDF text layer is language or mojibake. Python
(`src/mdmdoc/extract/plausibility.py`) is the reference; the ABAP twin
(`ZCL_MDMDOC_PDF=>plausibility` / `layer_usable`) must reproduce it exactly —
a scanned document whose embedded OCR layer is soup must be rejected on BOTH
sides, or SAP silently extracts bank details out of garbage (case C-2026-08-21-02).

Hand-porting an *approximation* is the failure mode this file exists to prevent:
a simplified ASCII-only variant scored the Korean bankbook mojibake 0.75, above
the 0.7 threshold, and would have missed the very bug the gate is for.

Emits into the ABAP repo (MDMDOC_ABAP_HOME or the sibling checkout):
  src/zcl_mdmdoc_plaus_golden.clas.abap   (data, "DO NOT EDIT", GEN-HASH header)
  src/zcl_mdmdoc_plaus_golden.clas.xml

Run:  uv run python tools/golden/gen_plausibility_golden.py
Check-only (CI / check_parity):  … --check   (exit 1 if the repo copy is stale)
"""
from __future__ import annotations

import hashlib
import os
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdmdoc.extract.plausibility import TRUST_LAYER, layer_usable, score_milli  # noqa: E402

ABAP_ROOT = Path(os.environ.get("MDMDOC_ABAP_HOME",
                                str(Path.home() / "Projects" / "mdm-doc-validator-abap")))

# ── the corpus ────────────────────────────────────────────────────────────────
# Every case is a real shape the gate must judge. Keep the texts SHORT — they are
# baked into an ABAP class — but keep them REAL: these are trimmed excerpts of
# actual text layers from bench/corpus, not invented strings.

CASES: list[tuple[str, str, str]] = [
    # (id, note, text)
    ("ko_mojibake",
     "Korean NH bankbook scan: embedded OCR layer turned Hangul into latin/symbol soup. "
     "1304 chars in the real file; ABAP trusted it because strlen >= 40. C-2026-08-21-02.",
     "   zt4fla  5()2-()655*    1994-A   1         d€qql€\n"
     "  q=€+    ;&l @..-{l   *\n"
     "           <<t 1.4€.ei>>\n"
     "                      r*+F_*                                g\n"
     "    7l'J 6t{] 'J  201.5H 0t e 22 \"J                   rll€tEJ+drHlEEtNo.1Account\n"
     "      E I       ; H       ; I    L I        = 6 t  l =      2015 Ll 01 C 22\";\n"
     "    7t'Jdru         (e) 055-566-7201                        58'J;flA   o\n"
     "    'JBEs *t!$t!'{B-fl<S>\n"
     "    drffi         ol8PlLtl      ol/trltL{l/Al/rl      u-t     Aoo"),

    ("font_soup",
     "Custom font without a ToUnicode CMap (Colombian bank certificate): every glyph "
     "extracts as punctuation. ABAP's ZCL_MDMDOC_PDF hits this whenever CMaps are missing.",
     "! \" # $ \"% &        '            #$(%\n"
     ")%*( +, -+ .,/%0%$(1%   2! (3  4 #%( #$(%(3 /# ( 0 % ( 0 5 #\n"
     "6789:;<8           =8>6789:;<8    ?@;AB>CD@7<:7B   EF<B98\n"
     "#%  %1            ,+..G        HGHI     (#J%\n"
     "KLMD87<BN<@O> #%(  #% (% 30 1%( $ (%%0 !  (# \" ( % % # \" #"),

    ("en_bank_letter", "Clean English bank confirmation letter — the baseline 'good' shape.",
     "Bank of America, N.A.\n222 Broadway, New York, NY 10038\n\n"
     "To whom it may concern,\n\nThis letter confirms that Acme Industrial Supplies LLC\n"
     "maintains checking account number 4830 2291 0077 with Bank of America.\n"
     "ABA routing (wires): 026009593   SWIFT: BOFAUS3N\nAccount opened: 12 March 2019\n"
     "Sincerely,\nJane Doe, Relationship Manager"),

    ("de_kontobestaetigung", "German Kontobestaetigung: umlauts and eszett must count as letters.",
     "Kontobestätigung\n\nSehr geehrte Damen und Herren,\n"
     "hiermit bestätigen wir, dass die Firma Müller & Söhne GmbH bei uns\n"
     "folgendes Konto unterhält:\nIBAN: DE89 3704 0044 0532 0130 00\nBIC: COBADEFFXXX\n"
     "Kontoinhaber: Müller & Söhne GmbH\nMit freundlichen Grüßen"),

    ("es_certificado", "Spanish certificacion bancaria (accents, long numbers with dots).",
     "CERTIFICACIÓN BANCARIA\nBancolombia S.A. certifica que el señor JUAN PÉREZ GÓMEZ,\n"
     "identificado con cédula de ciudadanía No. 1.020.345.678, es titular de la\n"
     "cuenta de ahorros No. 032-456789-01, activa desde el 03 de mayo de 2018."),

    ("fr_rib", "French RIB — short lines, many codes.",
     "RELEVÉ D'IDENTITÉ BANCAIRE\nTitulaire: SARL ATREEC\n"
     "IBAN: FR76 3000 4008 2800 0102 4567 890\nBIC: BNPAFRPPXXX\n"
     "Domiciliation: BNP PARIBAS PARIS OPERA\nCode banque 30004  Code guichet 00828"),

    ("ko_bankbook_real", "The SAME Korean bankbook, but read correctly (Hangul) — must pass.",
     "계좌번호 302-0653-1998-81\n예금종류 저축예금\n"
     "<<매직트리>>\n남상욕 님\n"
     "가입하신날 2013 년 01 월 22 일\n"
     "NH농협은행\nSWIFT CODE : NACFKRSE\n"
     "가입하신 점포 양산부산대병원<출>\n(☎) 055-366-7201"),

    ("ja_form", "Japanese bank-account form: kanji + katakana + latin labels.",
     "銀行口座認証\n銀行名: 三井住友銀行\n"
     "支店名: 神戸支店 (店番号 412)\n口座種別: 普通\n"
     "口座番号: 1234567\n口座名義人: カ）リリカラー"),

    ("zh_seal", "Chinese bank information sheet with a company seal.",
     "中国银行账户信息\n开户名称：四川省国际医学交流促进会\n"
     "开户银行：中国银行成都高新支行\n"
     "账号：1234 5678 9012 3456 789\n联行号：104651003456"),

    ("ru_receipt", "Russian payment receipt — Cyrillic plus long account numbers.",
     "КВИТАНЦИЯ № 000123\n"
     "Получатель: ООО «Ромашка»\n"
     "ИНН 7701234567 КПП 770101001\n"
     "Р/с 40702810400000001234 в ПАО Сбербанк\nБИК 044525225"),

    ("ar_cert", "Arabic VAT certificate (RTL script must not be penalised).",
     "شهادة تسجيل ضريبة القيمة المضافة\n"
     "اسم المكلف: شركة أطلس للسفر والسياحة\n"
     "الرقم الضريبي: 300123456700003\nVAT Registration Certificate"),

    ("w9_digital", "IRS W-9 digital fill — checkbox glyphs and a boxed EIN.",
     "Form W-9 (Rev. March 2024) Request for Taxpayer Identification Number and Certification\n"
     "1 Name of entity/individual: Qwest Corporation\n2 Business name: dba CenturyLink QC\n"
     "3a C corporation\n5 Address: 931 14th Street\n6 Denver, CO 80202\n"
     "Employer identification number 84-0273800\nSign Here  Date 1-6-26"),

    ("markdown_table", "A page that is mostly a table — pipes and dashes must not read as symbols.",
     "| Field | Value |\n|---|---|\n| Bank | JPMorgan Chase |\n| ABA | 071000013 |\n"
     "| Account | 2908805789 |\n| Name on the account | AECNS, PLLC |"),

    ("codes_only", "Almost no prose: IBAN, SWIFT, e-mail and a URL. The gate must still pass it.",
     "IBAN DE89370400440532013000\nBIC COBADEFFXXX\nEIN 84-0273800\n"
     "kundenservice@c24.de\nwww.c24.de\n069 24 24 69 000"),

    ("too_short", "Below the minimum character count — the pre-existing strlen gate.",
     "02/01/2026"),

    ("empty", "No text layer at all.", ""),
]


# ── ABAP emission (same conventions as gen_abap_golden.py) ────────────────────

def _lit(s: str) -> str:
    """A backtick ABAP string literal, chunked to keep lines <=140 chars
    (abaplint line_length). Backticks are doubled; embedded newlines become
    cl_abap_char_utilities=>newline concatenations."""
    out_parts: list[str] = []
    for si, seg in enumerate(s.split("\n")):
        if si:
            out_parts.append("cl_abap_char_utilities=>newline")
        seg = seg.replace("`", "``")
        i = 0
        while i < len(seg):
            out_parts.append(f"`{seg[i:i + 60]}`")
            i += 60
    if not out_parts:
        return "``"
    if len(out_parts) == 1:
        return out_parts[0]
    return " &&\n      ".join(out_parts)


def expected() -> list[dict]:
    rows = []
    for cid, note, text in CASES:
        usable, reason = layer_usable(text)
        rows.append({"id": cid, "note": note, "text": text,
                     "score": score_milli(text), "usable": usable, "reason": reason})
    return rows


def gen_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['id']}\x00{r['text']}\x00{r['score']}\x00{r['usable']}\x00".encode())
    h.update(f"trust={int(round(TRUST_LAYER * 1000))}".encode())
    return h.hexdigest()[:16]


HEADER = '''" GENERATED from tools/golden/gen_plausibility_golden.py (Python reference:
" src/mdmdoc/extract/plausibility.py) — *** DO NOT EDIT BY HAND ***
" Re-run the generator instead; tools/check_parity.py fails when this file is stale.
" GEN-HASH {h}
"
" Text-layer plausibility parity corpus. Each case is a real text-layer shape with the
" score the Python reference produces (0..1000) and its usable verdict. ZCL_MDMDOC_PDF
" must reproduce both — an approximate port is exactly the failure this guards against
" (case C-2026-08-21-02: a Korean bankbook scan whose mojibake OCR layer was trusted
" because it was longer than 40 characters).
CLASS zcl_mdmdoc_plaus_golden DEFINITION
  PUBLIC
  FINAL
  CREATE PRIVATE.

  PUBLIC SECTION.
    TYPES: BEGIN OF ty_case,
             id     TYPE string,
             note   TYPE string,
             text   TYPE string,
             score  TYPE i,          " Python plausibility( ) * 1000, rounded
             usable TYPE abap_bool,  " Python layer_usable( )[0]
           END OF ty_case.
    TYPES tt_cases TYPE STANDARD TABLE OF ty_case WITH EMPTY KEY.

    CONSTANTS c_gen_hash    TYPE string VALUE `{h}`.
    CONSTANTS c_trust_layer TYPE i      VALUE {trust}.

    CLASS-DATA gt_cases TYPE tt_cases READ-ONLY.
    CLASS-METHODS class_constructor.
ENDCLASS.


CLASS zcl_mdmdoc_plaus_golden IMPLEMENTATION.

  METHOD class_constructor.
    DATA ls_c TYPE ty_case.

{body}  ENDMETHOD.

ENDCLASS.
'''

CLAS_XML = """<?xml version="1.0" encoding="utf-8"?>
<abapGit version="v1.0.0" serializer="LCL_OBJECT_CLAS" serializer_version="v1.0.0">
 <asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">
  <asx:values>
   <VSEOCLASS>
    <CLSNAME>ZCL_MDMDOC_PLAUS_GOLDEN</CLSNAME>
    <LANGU>E</LANGU>
    <DESCRIPT>mdmdoc: GENERATED text-layer plausibility parity corpus (do not edit)</DESCRIPT>
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
    parts = []
    for r in rows:
        note_lines = textwrap.wrap(r["note"], width=104) or [""]
        comment = "\n".join(f"    \" {ln}" for ln in note_lines)
        parts.append(
            f"{comment}\n"
            f"    ls_c-id     = {_lit(r['id'])}.\n"
            f"    ls_c-note   = {_lit(note_lines[0])}.\n"
            f"    ls_c-text   = {_lit(r['text'])}.\n"
            f"    ls_c-score  = {r['score']}.\n"
            f"    ls_c-usable = {'abap_true' if r['usable'] else 'abap_false'}.\n"
            f"    APPEND ls_c TO gt_cases.\n")
    return HEADER.format(h=gen_hash(rows), trust=int(round(TRUST_LAYER * 1000)),
                         body="\n".join(parts))


def main() -> int:
    check = "--check" in sys.argv
    rows = expected()
    src = ABAP_ROOT / "src"
    if not src.is_dir():
        print(f"ABAP checkout not found at {ABAP_ROOT} "
              f"(set MDMDOC_ABAP_HOME)", file=sys.stderr)
        return 0 if check else 1
    abap = build_abap(rows)
    target = src / "zcl_mdmdoc_plaus_golden.clas.abap"
    if check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != abap:
            print(f"STALE: {target} does not match the Python reference — "
                  f"run `uv run python tools/golden/gen_plausibility_golden.py`", file=sys.stderr)
            return 1
        print(f"plausibility golden up to date ({len(rows)} cases, GEN-HASH {gen_hash(rows)})")
        return 0
    target.write_text(abap, encoding="utf-8")
    (src / "zcl_mdmdoc_plaus_golden.clas.xml").write_text(CLAS_XML, encoding="utf-8")
    longest = max(len(ln) for ln in abap.split("\n"))
    print(f"generated zcl_mdmdoc_plaus_golden ({len(rows)} cases, GEN-HASH {gen_hash(rows)}, "
          f"longest line {longest}) into {src}")
    for r in rows:
        print(f"  {r['id']:22s} score {r['score']:4d}  usable={str(r['usable']):5s}  {r['reason'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
