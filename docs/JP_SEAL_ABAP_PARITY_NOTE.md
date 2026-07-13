# ABAP parity note — JP bank-form extraction + seal (2026-07-13, Lilycolor)

Hand-port target: `~/Projects/mdm-doc-validator-abap/src/zcl_mdmdoc_extract.clas.abap`
(`fix_jp_form`, ~line 416) and `zcl_mdmdoc_norm`. `gen_rules_abap.py` ports rule DATA
only — this is predicate/extract LOGIC, so it is a manual mirror. Run `tools/check_parity.py`
after. **Do not** apply blindly: ABAP receives its text from SAP, not local tesseract, so
the OCR-language and inter-glyph-space concerns below may or may not occur there.

## Python changes made (source of truth: `src/mdmdoc/stage_b.py::_fix_jp_form`)

1. **`当座` → account_type** (mirror the existing `普通` block):
   if text contains `当座` and account_type empty → `当座預金 (current account)`.

2. **`銀行名`/`金融機関名` → bank_name** (fill-only): regex
   `(銀行名|金融機関名)\s*[:：]?\s*(<line value>)`, keep only if the value contains `銀行`.

3. **`口座名義` → account_holder** (fill-only): regex
   `口座名義(?:人)?\s*[:：]?\s*(<line value>)`, strip a trailing `様`/`御中`.

4. **`支店名` → branch_name** (NEW derived field, outside the model field contract —
   like `national_clearing`): regex `支店名\s*[:：]?\s*(<…支店>)`.
   - In ABAP `fix_jp_form` line 499, REMOVE `支店名` and the `Name/number|Name` alternatives
     from the NUMERIC `branch_code` regex — a NAME must never be captured as the 3-digit code.
     Keep only `(支店番号|支店コード|支店|Branch\s*(Number|No\.?|Code)?)…0?([0-9]{3})`.

5. **branch_code sanitize** (NEW, runs before the numeric rescue): if branch_code is set
   but not `\d{3,4}`, move it to branch_name (when empty) and clear branch_code, with
   cross-note `branch_code cleared: value was the 支店名 branch name, not a numeric 支店番号`.

6. **bank_address = company 住所 guard** (NEW): if bank_address is set and the text has
   both `住所` and `銀行名`, and the address value sits after a `住所` label but NOT near a
   `銀行` token → clear bank_address, cross-note `bank_address cleared: the 住所 on this
   form is the sender company's address, not the bank's`.

7. **inter-CJK space collapse**: Python collapses spaces tesseract inserts between CJK
   glyphs (`銀行 名` → `銀行名`) before matching (`ocr.collapse_cjk_spaces`, after NFKC).
   In ABAP this belongs in `zcl_mdmdoc_norm` alongside `translate_fullwidth`; only needed
   if the SAP text source also splits CJK glyphs. Add a `collapse_cjk_spaces` that removes
   `[ \t]+` between two CJK codepoints.

## Report
Python added a `Branch name` row (`report.py`). ABAP report/output should surface
`branch_name` similarly if it renders the field list.

## Signature / seal
The seal fix is in the Python Stage-A VISION probe (a page-level `pos-stamp` is decisive
for bank docs; `SIGNATURE_PROMPT` learned 角印/丸印). ABAP ZMDMDOC has no local vision
probe, so there is **no ABAP analog** — the `signed` flag arrives from its own source.
No rules.yaml change was needed (BNK-021 simply does not fire once signed=true).
