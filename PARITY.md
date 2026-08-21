# Python ↔ ABAP parity manifest

Two targets of one logic: **Python** (`~/Projects/mdm-doc-validator`) is the local
web tool + teach/eval + BTP side-service; **ABAP** (`~/Projects/mdm-doc-validator-abap`)
is the deterministic validator that runs inside S/4HANA/MDG. They interoperate via the
shared `mdmdoc.v1` result format and a shared rule source.

**What auto-syncs and what does not:**
- **Rule DATA** (`rules/*.yaml`) — single source of truth here; pushed to ABAP by the
  Python UI's `/rules/regenerate` → `gen_rules_abap.py` → `ZCL_MDMDOC_RULES_DATA`.
- **Predicate / extraction LOGIC** — hand-written in each language; `gen_rules_abap.py`
  does **NOT** carry it. Any change to a predicate body (e.g. the US-numeric-IBAN
  guard) must be hand-ported to ABAP. This file tracks that.

`tools/check_parity.py` enforces this file: it fails if the predicate list below drifts
from `predicates.REGISTRY`, if the rule YAML differs between the repos, or if anything
is listed under "Pending ABAP logic ports".

## Predicates (each must be implemented in BOTH `predicates.py` and `zcl_mdmdoc_rules`)
- unsigned_no_evidence
- unsigned_typed_block
- field_empty
- no_bank_ids
- swift_valid
- iban_valid
- ein_shape
- tin_type_vs_classification
- individual_with_business_name_and_ein
- line_swap_suspect
- date_older_than
- w8_ch4_cert_missing
- tin_structural
- tin_placeholder
- routing_format
- routing_checksum
- routing_prefix
- account_sig_digits

## Guards (stage_b deterministic guards ↔ `zcl_mdmdoc_extract` `[GUARD:x]` markers)
Statuses: `ported` (ABAP carries a `[GUARD:name]` marker in `zcl_mdmdoc_extract`),
`n/a` (Python-only by design — say why), `pending` (tracked drift → checker fails).
- audit_bank_ids — ported
- fix_jp_form — ported
- fix_statement_period — ported
- esignature_guard — ported
- drop_regulator_noise — ported
- drop_filename_echo — ported
- normalize_tin — ported
- ground_payment_instructions — ported
- drop_exemplar_echo — n/a (few-shot exemplar echo is a Python-LLM artifact; ABAP has no few-shot dataset)
- apply_w9_zone_probe — n/a (vision zone-crop probe; the ABAP path is deterministic/text-only)
- resolve_signature — n/a (vision-probe fold; its deterministic constituents —
  esignature_guard [extended to w9/w8] and officer_block_guard — ARE ported)
- officer_block_guard — ported
- finalize_provenance — n/a (Python report provenance structure; the ABAP result has no provenance field)
- corroborate_across_pages — n/a (needs per-page perception texts; ABAP receives one flat text and has no page structure)
- fix_zh_form — ported
- ground_account_holder — ported
- ground_bank_address — ported (G3: a labeled "Bank Address" line + the next
  City/State/Zip line fill an EMPTY bank_address — table-shaped remit forms
  print it verbatim while the model regularly skips it; never overwrites)
- annotate_bank_ids — n/a (G2/G4 console-notes layer over Python-ocr candidate
  keys — routing_suspect / swift_secondary / swift qualifiers — that the ABAP
  regex port does not produce; the strict routing/SWIFT FIELD logic is
  byte-identical on both sides, so nothing decision-relevant diverges)
- ground_doc_country — ported (F3: derived document country for `countries:`-scoped rules; sources bank_country → IBAN prefix → SWIFT cc → W-9⇒US / W-8⇒country_incorporation; the Python inventory-address fallback deliberately does NOT exist on either side)
- ground_national_clearing — ported
- collect_inventory — n/a (report/UI inventory layer + vault registration; pure
  label-anchored text regexes — portable later if the ABAP result gains an
  inventory table; the distinct_accounts FLAG rule BNK-031 ships in the shared
  rule data on both sides)
- record_settlement_issuer — n/a (folds the officer_block flag + issuer phrases
  into doc_subtype_evidence for the BNK-027 NOTE; the ABAP result struct has no
  subtype-evidence field — the flag-driven rule itself IS in the shared data)

## Guards (continued, 2026-07-09 accuracy wave)
- normalize_tin — ported (already listed)
- scrub_cjk_garbage — n/a (2026-07-13 handwriting-honesty layer: on a CJK/JP scan it blanks
  an identity field (bank_name/account_holder/branch_name) whose value is a script-mash or
  latin gibberish — the signature of OCR failing on HANDWRITING — so the operator sees '—' and
  the shared BNK-023 rule fires instead of a confident garbage value. Depends on the Python
  OCR/vision perception of a scanned handwritten form, which the ABAP twin does not have; the
  verdict itself rides the shared BNK-023/BNK-050 rule DATA)
The signature 3-state change (band/page/text votes + `uncertain`) is inside the
existing `resolve_signature` (n/a — vision). The confidence gate (`confidence.py`
→ CONF-001) is a Python pipeline layer, not a stage_b guard; ABAP corp v1 is
deterministic-only and does not carry it (documented n/a — no marker expected).
MDMDOC_NOW (audit C13) is a Python-only test clock for `date_older_than`; the
ABAP side stays on sy-datum by design — no port expected. The Python
`_DATE_FORMATS` day-first-abbreviated additions of the same commit CONVERGE
Python toward ABAP's `try_textual_date` (which already parsed "15 Jan 2023").

## Perception & controller layer (Python-only by design) — 2026-07-10
The quality wave's perception layer has NO ABAP counterpart and never will:
ABAP receives one flat text (CONTRACT.md) and has no pages, renders or vision.
Covered: vision payload caps (model_client), the text-layer garbage gate
(ocr.text_layer_garbage — reroute only; the ABAP caller supplies text it
already trusts), page bookkeeping (w9_pages/survey_texts), W-8 page targeting,
the signature vision ensemble (stage_a.signature_probe), and the evidence
ladder (ladder.py — the pipeline controller's bounded second perception pass).
None of these are stage_b guards except `corroborate_across_pages` (listed
n/a above); nothing here is a pending port. The operator-console trust layer
(E-wave 2026-07-10: `oplog.py` audit ledger, `challenges.py` rule-challenge
ledger, gate-panel/mark-valid/finding-vote endpoints; F-wave additions:
`undo.py`, rules/history + labels_history snapshots, teach-type,
`doctype_profiles.py` pattern memory) is equally
Python-console-only — the ABAP twin has no operator console; rule DATA changes
made through it (delete/tier) reach ABAP through the normal `gen_rules_abap`
regeneration, nothing else to port.

## Bulk table validation (V-wave, Python-console-only) — 2026-07-10
`src/mdmdoc/bulk/` (mass row-bucket validation of SAP table exports: bank /
tax / postal-region) is a SEPARATE product surface with NO ABAP counterpart
planned: it validates operator-uploaded spreadsheets in the local console,
outside the document pipeline and outside the rule-approval gate (row buckets
are audit facts with cited BULK-### rules, not processing verdicts). Its data
files rules/bulk_{tax,bank,postal}.yaml are NOT part of the shared rule DATA
that gen_rules_abap.py carries. The only shared logic is rules/bankmath.py —
already covered by the document-rules parity entries above.

## Golden parity corpus (behavioural, beyond marker grep) — 2026-07-09, v2 2026-07-10
`tools/golden/golden_cases.json` (**11 cases**, corpus v2 with `llm_fields` +
`expect.verdict`/`expect.findings`) runs through the Python DETERMINISTIC engine
(`tools/golden/run_golden.py`, `tests/test_golden_parity.py`) AND through
`run_rules(enforce_approvals=False)+decide()` for verdict parity. The ABAP twin
**IS generated**: `tools/golden/gen_abap_golden.py` emits
`zcl_mdmdoc_golden_data` (headers `GOLDEN-HASH 8a74945f3e2d6b66` = corpus hash,
`GEN-HASH 332a326a58f70960` = generator+runner hash) and the hand-written loop
testclass calls `zcl_mdmdoc_extract=>build` + `zcl_mdmdoc_rules->run()` +
`zcl_mdmdoc_verdict=>decide()`, asserting fields, crosscheck-note substrings,
verdict and finding ids. check_parity **§7 enforces it**: corpus-hash match +
GEN-HASH match + regenerate-and-diff (catches hand edits, generator drift and a
stale baked corpus). Update flow: edit cases → run gen_abap_golden.py → commit
both repos (ABAP first, then the pin bump).

## Constants parity (§8, audit M5)
Hand-duplicated constants (regexes, thresholds, phrase lists) are registered in
`tools/parity/constants.json` and marked `[CONST:id]` at both source sites;
check_parity §8 extracts and compares them (canon `same`) or pins each side's
literal (canon `pinned`, for dialect-divergent regexes). Registered now (29):
BIC/IBAN shapes, 4 OCR id-regexes, SWIFT-lengths/EIN-digits/years defaults,
_EV_POSITIVE, no_bank_ids keys, ES/DE month map, ABAP month table, Python date
formats, ZH letter/label sets, officer/signatory phrase sets, and the 2026-07-10
TIN-structure pack (ein_never_prefixes, known_fake_tins, ssn_area_invalid,
itin_group_ranges, tin_format_shapes — W9-040/041, shared with the /ui/tax
bulk tab via tin_bulk.py). Backlog to register incrementally (each = marker + manifest entry):
business-suffix regex, _BANKNAME_NOISE, person-name regex + stopwords,
classification keywords, e-sig markers, JP markers + 口座番号/postal regexes,
statement-period regex, COUNTRY_NAME_TO_ISO map, verdict precedence/next_step.
Known §8-documented gap: the ABAP EIN/SSN regex lacks Python's hyphen-adjacency
guards (phantom-EIN fix) — port candidate, tracked in the manifest note.

## Pending ABAP logic ports (Python has it, ABAP not yet — remove the line once applied)

- **Vision model default** (`p_ovis`, currently documented as `qwen2.5vl:7b`).
  To be set from the benchmark winner once `bench/DECISION.md` is written — the ABAP
  twin reaches only its own PDF text layer plus Ollama, so the model choice IS the
  extraction quality there.

## Ported extraction logic (Python has it, ABAP carries it, both are pinned)

- **Text-layer plausibility gate** — `src/mdmdoc/extract/plausibility.py` →
  `ZCL_MDMDOC_PDF=>plausibility` / `layer_usable` (ABAP commit `d8b147c`, 2026-08-21).
  Judges whether a PDF text layer is language or mojibake, so a scan whose embedded
  OCR layer is soup is treated as a scan instead of being mined for bank details
  (case C-2026-08-21-02). Deliberately free of unicode character tables — a character
  outside printable ASCII that is not one of the explicit symbols counts as a letter
  of another script — because 7.50 has no such tables and an approximate port fails:
  a simplified variant scored the Korean mojibake 0.75 against a 0.7 threshold.
  Parity is asserted, not assumed: `ZCL_MDMDOC_PLAUS_GOLDEN` is generated by
  `tools/golden/gen_plausibility_golden.py` and pins 16 real text-layer shapes with
  the score (0..1000) and verdict the Python reference produces; `ltcl_pdf` loops
  over it. Both sides count ASCII letters only for the vowel ratio and word
  extraction — the German case (913 vs 906) proved that umlauts must not diverge.
  Change either side → re-run the generator, or the ABAP unit test goes red.

## Auto-synced DATA (never hand-edit the ABAP copy)

- **Rule data** — `rules/*.yaml` → `ZCL_MDMDOC_RULES_DATA` via the ABAP repo's
  `tools/gen_rules_abap.py` (or the Python UI's `/rules/regenerate`).
- **Golden parity corpora** — `tools/golden/gen_abap_golden.py` → `ZCL_MDMDOC_GOLDEN_DATA`;
  `tools/golden/gen_plausibility_golden.py` → `ZCL_MDMDOC_PLAUS_GOLDEN`.
- **Model prompts** — `prompts/vision/*.txt` → `ZCL_MDMDOC_PROMPTS` via
  `tools/gen_prompts_abap.py` (ABAP commit `2b6cf03`). `ZCL_MDMDOC_LLM=>system_vision`
  reads it instead of carrying its own text: the three sentences it used to hold
  lacked "never translate or romanize", and without that rule a CJK page comes back
  in latin letters with every value on it lost.


  To be set from the benchmark winner once `bench/DECISION.md` is written — the ABAP
  twin reaches only its own PDF text layer plus Ollama, so the model choice IS the
  extraction quality there.

## Coordination requests → rules-editor/ABAP session (2026-07-07, audit milestone)
1. ~~US-IBAN port reminder~~ — **closed 2026-07-09**: applied by the audit session
   (ABAP tree was clean and the session inactive for 3 days, per the agreed rule).
4. **add_cr_note (Data Owner / approver note) — NEW handoff 2026-07-09**: full spec in
   `docs/SAP_READINESS.md` §7 (exact signature, graceful-fallback behavior mirroring the
   GOS attachment template, candidate mechanisms all marked investigate-on-system).
   Implementation is on-system ABAP work; nothing in either repo asserts a note API exists.
2. **`rules_io.save_rules` must preserve UNKNOWN yaml keys per rule on any round-trip**
   (incl. the Approvals panel "Correct" path). The audit milestone adds per-rule
   provenance metadata `tier: corp|experimental|learned` and `source: skill|policy|operator`
   — if a save strips unknown keys, governance data is silently lost. A rule edited via
   the panel must come back with its `tier`/`source` intact.
3. ~~`gen_rules_abap.py`: treat `tier`/`source` as additive/optional~~ — **closed
   2026-07-10 (audit R3)**: `tier` now propagates into `ZCL_MDMDOC_RULES_DATA` and
   the emitted `rules/*.json`; `--tier-min corp` implements the corp shipping
   profile; unknown keys never fail generation; `rule_hash` excludes tier/source
   (metadata — approvals survive tier edits).
5. **П3 evidence-rescue (2026-07-09 audit wave)**: `ground_payment_instructions` gained a
   rescue sub-path backed by `doctype_evidence.score` (pure, table-driven AND-gate:
   letter shape + named-bank identity + account facts + holder signal → bank_letter with
   `doc_type_uncertain`). NOT yet ported; ABAP keeps the stricter grounding
   (other → NMR) — a safe temporary divergence. Port target: table + AND-gate inside
   `zcl_mdmdoc_extract` `[GUARD:ground_payment_instructions]`.
