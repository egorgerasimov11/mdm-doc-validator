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
- drop_exemplar_echo — n/a (few-shot exemplar echo is a Python-LLM artifact; ABAP has no few-shot dataset)
- apply_w9_zone_probe — n/a (vision zone-crop probe; the ABAP path is deterministic/text-only)
- apply_signature_probe — n/a (vision signature probe; same reason)
- finalize_provenance — n/a (Python report provenance structure; the ABAP result has no provenance field)

## Pending ABAP logic ports (Python has it, ABAP not yet — remove the line once applied)
(none — the US numeric-IBAN guard was applied to `p_iban_valid` + test
`pred_iban_numeric_us_ok` on 2026-07-09, together with the wave-5 extract-guard pack.)

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
3. **`gen_rules_abap.py`: treat `tier`/`source` as additive/optional** — either propagate
   into `ZCL_MDMDOC_RULES_DATA` (preferred: enables "corp profile = only tier:corp rules"
   in ZMDMDOC) or ignore them; must not fail on unknown keys. `tools/check_parity.py`
   will treat these fields as non-verdict metadata (audit session keeps that file).
