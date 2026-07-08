# One version: how the Python validator and the ABAP twin stay in sync

Two hand-written targets of one logic — **Python** (this repo: web panel,
teach/eval, BTP side-service) and **ABAP** (`abap/` submodule ↔
`mdm-doc-validator-abap`: the deterministic validator inside S/4HANA/MDG).
They cannot auto-generate each other; this page says exactly what syncs
automatically, what is hand-ported with receipts, and how the drift detector
keeps both honest. The enforcement lives in `tools/check_parity.py` (run it
manually, pre-commit, or in CI — non-zero exit = drift).

## What syncs AUTOMATICALLY

| Artifact | Mechanism |
|---|---|
| Rule DATA (`rules/banking.yaml`, `rules/w9.yaml`) | Single source of truth is THIS repo. The panel's `/rules/regenerate` (or `tools/gen_rules_abap.py`) copies the YAML into the ABAP repo and regenerates `ZCL_MDMDOC_RULES_DATA`. DATA only — never logic. |
| Rule metadata (`tier:`, `source:`) | Carried with the YAML; `gen_rules_abap.py` treats unknown keys as additive/optional (never a failure). |

## What is HAND-PORTED (with receipts the checker greps for)

| Logic | Python home | ABAP home | Receipt |
|---|---|---|---|
| Rule predicates | `rules/predicates.py` `REGISTRY` | `zcl_mdmdoc_rules` WHEN-dispatch | predicate-surface diff + `PARITY.md ## Predicates` |
| Deterministic guards | `stage_b.py` module-level `_guard(ext[, raw])` | `zcl_mdmdoc_extract` | `[GUARD:<name>]` marker + `PARITY.md ## Guards` status (`ported` / `n/a` / `pending`) |

Changing a predicate body or a guard in Python? Port it to ABAP in the same
change (or add a `pending` line in `PARITY.md` — the checker fails loudly until
the port lands, which is the point: drift is tracked, never silent).

## The `abap/` submodule = the "one version" pin

One checkout of this repo carries the exact ABAP twin it was verified against:

```bash
git clone --recurse-submodules <this-repo>       # fresh clone
git submodule update --init                      # existing clone
```

**Bumping the pin** after ABAP commits land:

```bash
(cd abap && git pull origin main) && git add abap && git commit -m "abap: bump submodule pin"
```

`check_parity.py` fails when the pin differs from the live ABAP checkout's HEAD
(on machines that have both — the dev laptops). Resolution order for the ABAP
sources the checker reads: `MDMDOC_ABAP_HOME` env → live sibling checkout
`~/Projects/mdm-doc-validator-abap` → the `abap/` submodule.

**On the mini / production**: do NOT init the submodule. Both repos are
private and the mini's deploy key is scoped to this repo only; the runtime
never reads ABAP sources. Plain `git pull` works fine with an uninitialized
submodule (the `mdmdoc-deploy` skill flow is unchanged).

## Cross-session coordination

The ABAP repo is co-owned by a parallel rules-editor/ABAP session. Handoffs go
through `PARITY.md` ("Coordination requests" section), never through editing
the other session's uncommitted files. Commit small and early on both sides.
