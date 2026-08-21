"""Text-layer plausibility gate: garbage layers must fail, real text must pass."""
from pathlib import Path

import fitz
import pytest

from mdmdoc import config
from mdmdoc.extract.plausibility import TRUST_LAYER, features, layer_usable, plausibility

# The literal text layer of the Korean bankbook scan (doct/Bank account_Pf_Nam.PDF):
# an embedded OCR layer that turned Hangul into latin/symbol soup. It passed
# ocr.text_layer_garbage and the pipeline trusted it — C-2026-08-21-02.
KOREAN_MOJIBAKE = (
    "   zt4fla  5()2-()655*    1994-A   1         d€qql€\n"
    "  q=€+    ;&l @..-{l   *\n"
    "           <<t 1.4€.ei>>\n"
    "                      r*+F_*                                g\n"
    "    7l'J 6t{] 'J  201.5H 0t e 22 \"J                   rll€tEJ+drHlEEtNo.1Account\n"
    "      E I       ; H       ; I    L I        = 6 t  l =      2015 Ll 01 C 22\";"
    "                                 *4 aqir'J  *,Tree\n"
    "                                 UEg=g\n"
    "                                 SWIFT CoDE : NACFKRSE\n"
    "    7t'Jdru\n         (e) 055-566-7201                                                 58'J;flA   o\n"
    "    'JBEs *t!$t!'{B-fl<S>\n-                      +---                                        -\n"
    "    drffi         ol8PlLtl\n                        ol/trltL{l/Al/rl\n"
    "                        u-t     Aoo                                    .J',ell\n"
    "             . banking.nonghyup.com                      . E+ 01r]^1Lf\n"
    "             '45681, olill ql 4;/EJEU+,                   1588-2100,15t+4-2100\n"
    "    rlq tse3B= ol$u +^1^1#^l=grLltrl.                o E =E7lgolqE 5 iilflei IESII+ trlt6t1lE|: 3?.\n"
    "    1. 0l 7laflB^1183\n                   5801olLltre,qEzelts d^E€(?.!ElLJl            sdEJflrn Ee-c'Llcf.\n"
    "      bJa E;7lE 5)g 0lg6t^10f gLltl.                 o 48el ol^fal =s'3e+80lIfelqEE-d8^f7f86i: Ol^f\n"
)

# A custom-font layer without ToUnicode (dataset/corpus/bank/…bancolombia.pdf shape)
FONT_SOUP = (
    '! " # $ "% &        \'            #$(%\n)%*( +, -+ .,/%0%$(1%   2! (3  4 #%( #$(%(3 /# ( 0 % ( 0 5 #\n'
    "6789:;<8           =8>6789:;<8    ?@;AB>CD@7<:7B   EF<B98\n#%  %1            ,+..G        HGHI     (#J%\n"
    'KLMD87<BN<@O> #%(  #% (% 30 1%( $ (%%0 !  (# " ( % % # " #\n'
)

REAL_TEXTS = {
    "en_bank_letter": (
        "Bank of America, N.A.\n222 Broadway, New York, NY 10038\n\n"
        "To whom it may concern,\n\nThis letter confirms that Acme Industrial Supplies LLC "
        "maintains checking account number 4830 2291 0077 with Bank of America.\n"
        "ABA routing (wires): 026009593   SWIFT: BOFAUS3N\nAccount opened: 12 March 2019\n"
        "Sincerely,\nJane Doe, Relationship Manager\n"
    ),
    "de_kontobestaetigung": (
        "Kontobestätigung\n\nSehr geehrte Damen und Herren,\nhiermit bestätigen wir, dass die "
        "Firma Müller & Söhne GmbH bei uns folgendes Konto unterhält:\nIBAN: DE89 3704 0044 0532 "
        "0130 00\nBIC: COBADEFFXXX\nKontoinhaber: Müller & Söhne GmbH\nMit freundlichen Grüßen\n"
    ),
    "es_certificado": (
        "CERTIFICACIÓN BANCARIA\nBancolombia S.A. certifica que el señor JUAN PÉREZ GÓMEZ, "
        "identificado con cédula de ciudadanía No. 1.020.345.678, es titular de la cuenta de "
        "ahorros No. 032-456789-01, la cual se encuentra activa desde el 03 de mayo de 2018.\n"
    ),
    "ko_bankbook": (
        "계좌번호 302-0653-1998-81\n예금종류 저축예금\n<<매직트리>>\n남상욱 님\n"
        "가입하신날 2013 년 01 월 22 일\n발행한날 2013 년 01 월 22 일\nNH농협은행\nSWIFT CODE : NACFKRSE\n"
        "가입하신 점포 양산부산대병원<출>\n(☎) 055-366-7201\n"
    ),
    "ja_form": (
        "銀行口座認証\n銀行名: 三井住友銀行\n支店名: 神戸支店 (店番号 412)\n口座種別: 普通\n"
        "口座番号: 1234567\n口座名義人: カ）リリカラー\nフリガナ: カ）リリカラー\n"
    ),
    "zh_seal": (
        "中国银行账户信息\n开户名称：四川省国际医学交流促进会\n开户银行：中国银行成都高新支行\n"
        "账号：1234 5678 9012 3456 789\n联行号：104651003456\n（加盖公章）\n"
    ),
    "ru_receipt": (
        "КВИТАНЦИЯ № 000123\nПолучатель: ООО «Ромашка»\nИНН 7701234567 КПП 770101001\n"
        "Р/с 40702810400000001234 в ПАО Сбербанк\nБИК 044525225\nСумма: 12 500,00 руб.\n"
    ),
    "ar_cert": (
        "شهادة تسجيل ضريبة القيمة المضافة\nاسم المكلف: شركة أطلس للسفر والسياحة\n"
        "الرقم الضريبي: 300123456700003\nتاريخ الإصدار: 2026/03/31\nVAT Registration Certificate\n"
    ),
    "w9_digital": (
        "Form W-9 (Rev. March 2024) Request for Taxpayer Identification Number and Certification\n"
        "1 Name of entity/individual: Qwest Corporation\n2 Business name: dba CenturyLink QC\n"
        "3a C corporation ☑\n5 Address: 931 14th Street\n6 Denver, CO 80202\n"
        "Employer identification number 84-0273800\nSign Here  Date 1-6-26\n"
    ),
}


def test_garbage_layers_fail():
    assert plausibility(KOREAN_MOJIBAKE) < TRUST_LAYER
    assert plausibility(FONT_SOUP) < TRUST_LAYER
    ok, why = layer_usable(KOREAN_MOJIBAKE)
    assert not ok and "implausible" in why


@pytest.mark.parametrize("name", sorted(REAL_TEXTS))
def test_real_text_passes(name):
    score = plausibility(REAL_TEXTS[name])
    assert score >= 0.85, (name, score, features(REAL_TEXTS[name]))
    assert layer_usable(REAL_TEXTS[name])[0]


def test_empty_and_short():
    assert plausibility("") == 0.0
    ok, why = layer_usable("abc")
    assert not ok and "chars" in why


def test_margin_between_garbage_and_real():
    worst_real = min(plausibility(t) for t in REAL_TEXTS.values())
    best_garbage = max(plausibility(KOREAN_MOJIBAKE), plausibility(FONT_SOUP))
    assert worst_real - best_garbage >= 0.25


def _layer(p: Path, n: int = 3) -> str:
    with fitz.open(p) as d:
        return "\n".join(d[i].get_text("text", sort=True) for i in range(min(d.page_count, n)))


@pytest.mark.skipif(not (config.EVAL_DIR / "synthetic" / "docs").exists(),
                    reason="synthetic corpus not present")
def test_every_synthetic_layer_passes():
    docs = sorted((config.EVAL_DIR / "synthetic" / "docs").glob("*.pdf"))
    assert docs
    low = [(p.name, plausibility(_layer(p))) for p in docs
           if len(_layer(p).strip()) >= 40 and plausibility(_layer(p)) < 0.85]
    assert not low, low
