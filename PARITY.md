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

## Pending ABAP logic ports (Python has it, ABAP not yet — remove the line once applied)
- iban_valid: US numeric-IBAN guard — a purely numeric value in the iban field (US and
  other non-IBAN countries) is a plain account number, not a malformed IBAN, so it must
  not fire BNK-011. Hand-off snippet (p_iban_valid `CO '0123456789'` guard + unit test
  pred_iban_numeric_us_ok) is in ~/.claude/plans/cli-tidy-globe.md. Owned by the ABAP session.
