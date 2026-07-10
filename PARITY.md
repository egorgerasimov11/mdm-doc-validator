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

## Guards (continued, 2026-07-09 accuracy wave)
- normalize_tin — ported (already listed)
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
n/a above); nothing here is a pending port.

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
literal (canon `pinned`, for dialect-divergent regexes). Registered now (14):
BIC/IBAN shapes, 4 OCR id-regexes, SWIFT-lengths/EIN-digits/years defaults,
_EV_POSITIVE, no_bank_ids keys, ES/DE month map, ABAP month table, Python date
formats. Backlog to register incrementally (each = marker + manifest entry):
business-suffix regex, _BANKNAME_NOISE, person-name regex + stopwords,
classification keywords, e-sig markers, JP markers + 口座番号/postal regexes,
statement-period regex, COUNTRY_NAME_TO_ISO map, verdict precedence/next_step.
Known §8-documented gap: the ABAP EIN/SSN regex lacks Python's hyphen-adjacency
guards (phantom-EIN fix) — port candidate, tracked in the manifest note.

## Pending ABAP logic ports (Python has it, ABAP not yet — remove the line once applied)
(none — the US numeric-IBAN guard was applied to `p_iban_valid` + test
`pred_iban_numeric_us_ok` on 2026-07-09, together with the wave-5 extract-guard pack.
The golden ABAP twin above is data/test generation, not a logic port.)

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
