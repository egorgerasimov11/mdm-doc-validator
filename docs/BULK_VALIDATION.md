# Bulk validation — mass checks over SAP master-data tables

The Bulk tab (`/ui/bulk`, CLI `mdmdoc bulk`) validates THOUSANDS of rows at
once. It is a separate product surface from the document pipeline: the input
is a **table**, the output is a **per-row bucket** with cited rules — never
the document ACCEPT/REJECT fold, and deliberately outside the document-rules
approval gate (these are audit facts about the data itself).

## The flow

```
raw SAP exports (SE16N)                     canonical template
  BUT0BK / tax numbers / ADRC …               templates/bulk/*.xlsx
        │                                          │
        │  (a) drop directly — descriptive         │  (b) fill yourself, or let
        │      headers are recognized              │      the mdmdoc-bulk-feed
        └────────────────┬─────────────────────────┘      skill fill it from
                         ▼                                 raw exports
                /ui/bulk  or  mdmdoc bulk <file>
                         ▼
   per-row buckets: VALID / SUSPICIOUS / INVALID / SKIPPED
                         ▼
   inbox/<id>__<case>_validated.xlsx   ← your data + Verdict/Rule IDs/Reasons
   runs/<id>/bulk_report.{json,md}     ← masked summary (leak-gated)
```

## Buckets

| Bucket | Meaning |
|---|---|
| VALID | every applicable deterministic check passed |
| SUSPICIOUS | formally passing but needs a human look (duplicates, registry miss, empty-but-expected) |
| INVALID | a deterministic check FAILED — checksum / format / wrong-country value; mathematically or structurally wrong |
| SKIPPED | cannot be judged (masked in the export `XXXXXXX`, empty row) |

Buckets are best-effort audit output for a human review (Approve/Reject/
Correct) — the manual gate stays; nothing is written back to SAP.

## Case 1 — Banking (BUT0BK)

Template: `templates/bulk/banking_template.xlsx`. Raw SE16N BUT0BK exports
(Business Partner | Bank Country/Region | Bank Key | Bank acct | …) parse
directly.

| Rule | Check | Bucket | Basis |
|---|---|---|---|
| BULK-B01 | US bank key = exactly 9 digits (BIC in the key field is named) | INVALID | SAP BANKL-US convention |
| BULK-B02 | ABA 3-7-1 mod-10 checksum | INVALID | ABA routing standard |
| BULK-B03 | Fed routing prefix ∈ 00, 01-12, 21-32, 61-72, 80 | INVALID | Federal Reserve routing symbols |
| BULK-B04 | account all zeros / <4 significant digits (leading zeros = SAP padding) | INVALID / SUSP | Nacha practice |
| BULK-B05 | IBAN mod-97 + national length | INVALID | ISO 13616 |
| BULK-B06 | IBAN prefix vs Bank Country | SUSPICIOUS | consistency |
| BULK-B07 | SWIFT shape / country | INVALID / SUSP | ISO 9362 |
| BULK-B08 | control key convention (US 01 checking / 02 savings) | SUSPICIOUS | SAP BKONT |
| BULK-B09 | same account under several partners | SUSPICIOUS | duplicate master data |
| BULK-B10 | national key shapes (DE BLZ 8, GB sort 6, AU BSB 6, IN IFSC, CN CNAPS 12 …) | INVALID | national systems |
| BULK-B11 | empty/masked account | SKIPPED | — |
| BULK-B12 | **web option**: routing not listed in usbanklocations → paymentlabs → wise (unique routings, cached in `inbox/bulk_cache_routing.json`) | SUSPICIOUS | live directories; only the routing number leaves the machine |

## Case 2 — Tax numbers (BP tax categories)

Template: `templates/bulk/tax_template.xlsx`. Raw exports
(Business Partner | Tax Number Category | Tax number | Tax Number Long) parse
directly. Catalog: `rules/bulk_tax.yaml` (~45 countries — add local/custom
categories there).

**US doctrine:** the category digit (US0..US4) carries NO authoritative
SSN/EIN mapping — every US value is judged by the number's own structure
(valid if ANY of SSN / ITIN / EIN passes). Same doctrine as the `/ui/tax` tab.

| Rule | Check | Bucket |
|---|---|---|
| BULK-T01 | category not in the catalog — judged by shape only | SUSPICIOUS |
| BULK-T02 | value fails the category's format/structure | INVALID |
| BULK-T03 | value is ANOTHER country's number (national prefix + shape + checksum proof) — e.g. a German VAT under US0 | INVALID |
| BULK-T04 | national checksum failed (DE ISO-7064, IT, FR key, ES NIF/CIF, PL, BE, AT, GB 97/9755, CH, ABN, CNPJ/CPF, CN USCC, IN GSTIN, …) | INVALID |
| BULK-T05 | masked in the export (`XXXXXXX`, `****1234`) | SKIPPED |
| BULK-T06 | same number under several partners | SUSPICIOUS |
| BULK-T07 | empty value | SKIPPED |
| BULK-T08 | Country column disagrees with the category's country | SUSPICIOUS |

## Case 3 — Postal / region (addresses)

Template: `templates/bulk/postal_region_template.xlsx`.
**Attach references to the run** (`+ reference` on the Bulk page):
a **T005S** export enables region-membership checks; **T005U** (optional)
adds region descriptions to reasons. Without T005S only postal formats run
(the result says so).

| Rule | Check | Bucket |
|---|---|---|
| BULK-R01 | country not ISO-mappable | SUSPICIOUS |
| BULK-R02 | region not in T005S for the country (valid samples cited) | INVALID |
| BULK-R03 | region empty though the country has regions (region-required countries → INVALID: US CA AU BR MX IN CN JP IT ES) | INVALID / SUSP |
| BULK-R04 | postal code fails the country format (`rules/bulk_postal.yaml`, ~85 countries) | INVALID |
| BULK-R05 | postal code in a no-postal country (AE/HK/QA…) | SUSPICIOUS |
| BULK-R06 | placeholders (`Foreign`, `99`, `XX`, `000…`) | SUSPICIOUS |
| BULK-R07 | empty row | SKIPPED |

## Privacy

* The **validated workbook** (inbox/) carries FULL values — it is the
  operator's own uploaded data on the local instance (same posture as the
  `/ui/tax` and `/address` tabs). `inbox/` is gitignored and never synced.
* Everything under `runs/` is **masked**: reasons are constructed mask-only
  (`XX-XXX4589`), and `runstore.write`'s strict leak gate is the backstop.
* Web layer egress: the ROUTING NUMBER only — a public identifier; cached so
  re-runs are offline.

## Feeding data with Claude (skill `mdmdoc-bulk-feed`)

The companion skill takes RAW SAP exports (even several files), joins them by
Business Partner where needed, fills the canonical template with a script
(no retyping, no invented values) and hands the file to the Bulk tab or
`POST /api/v1/bulk`. See `Agent/skills/mdmdoc-bulk-feed/SKILL.md`.

## Extending

* New tax categories/countries: edit `rules/bulk_tax.yaml` (regex + optional
  `checksum:` from `bulk/taxmath.REGISTRY`).
* New bank-key shapes / control keys: `rules/bulk_bank.yaml`.
* New postal formats / region-required countries: `rules/bulk_postal.yaml`.
* New case: implement `bulk/<case>.py: check_rows`, register aliases +
  markers in `bulk/reader.py`, dispatch in `bulk/engine.py`, add a template
  in `tools/gen_bulk_templates.py`.
