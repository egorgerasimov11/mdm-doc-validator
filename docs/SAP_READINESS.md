# SAP Import Readiness — ZMDMDOC (ABAP validator) on S/4HANA/MDG

Status list for importing the ABAP clone of mdm-doc-validator
(repo `~/Projects/mdm-doc-validator-abap`, mirrored here as the `abap/` submodule)
into a target S/4HANA / MDG system.

Primary sources: `abap/docs/INTEGRATION.md` (chapters 0-13), `abap/docs/CONTRACT.md`,
`abap/README.md`, the `src/` object inventory, `PARITY.md` and `tools/check_parity.py`
in this repo. Every claim below traces to one of those; nothing is asserted about the
target SAP system beyond what the repo itself states.

---

## 1. Purpose and how to read this document

This is the single answer to: **"What is already DONE and verified locally, and what
MUST still be confirmed on the target SAP system before go-live?"**

Every item carries one of three statuses:

| Marker | Meaning |
|---|---|
| ✅ **done** | Implemented and verified locally (abaplint, ABAP Unit inventory, parity gate). No on-system work expected beyond activating it. |
| ⚠️ **verify-on-system** | Code or procedure exists, but its correctness depends on the target system (kernel behavior, MDG release, installed components, customizing). Must be checked on the real system; the repo flags these itself. |
| ❌ **not-implemented** | Deliberately absent (not ported, or specified for a future on-system session). Do not expect it to work. |

Rule of thumb from the repo (INTEGRATION.md ch.11): **while any pre-flight check is
red, do NOT enable the BAdI.** The intended path is: import → activate → ABAP Unit
green → `ZMDMDOC_SETUP` GO → only then MDG BAdI activation.

Target environment (INTEGRATION.md header): on-premise SAP, **ABAP >= 7.50**
(ECC EhP8, any S/4HANA, ABAP Platform). **NOT** for BTP ABAP Environment
(Steampunk) — no SAPGUI file-system access there. Classic regex only, no PCRE
(7.50 compatibility, per CONTRACT.md conventions).

---

## 2. What already works locally ✅

These are verified on the developer side, off-system:

- ✅ **abaplint: 0 issues on 7.50 syntax.** Static check via `npx --yes @abaplint/cli`
  against `abaplint.json` (target v750). SEVEN objects are deliberately **excluded**
  from offline abaplint because they use MDG framework types unavailable off-system:
  the classes `ZCL_MDMDOC_MDG_READER`, `ZCL_MDG_BP_FIELD_DERR_VAL`,
  `ZCL_MDMDOC_MDG_MAP`, `ZCL_MDMDOC_ONBOARD` and the programs
  `ZMDMDOC_MDG_DISCOVER`, `ZMDMDOC_DOCTOR`, `ZMDMDOC_SETUP`. That means the whole
  setup/doctor/onboard onboarding path is NOT statically lint-checked offline — it is
  itself verify-on-system for syntax. All seven carry
  `*** VERIFY ON SYSTEM ***` in their source headers.
  ABAP Unit itself can only run on a system — see section 5.

- ✅ **ABAP Unit test inventory: 204 test methods** (as of 2026-07-10, recounted
  directly from the `*.testclasses.abap` files in `src/` — non-comment `FOR TESTING`
  statements minus the `DEFINITION FOR TESTING` class lines), local test classes on
  each class, all `RISK LEVEL HARMLESS DURATION SHORT`, no network/filesystem/GUI.
  The count grows with every wave — recount before quoting it. Per-class:

  | Class | Tests | Covers |
  |---|---|---|
  | ZCL_MDMDOC_RULES | 29 | rule engine, all when-operators, predicates, RU messages, JSON override, ENGINE-GUARD fail-closed |
  | ZCL_MDMDOC_MASK | 23 | SSN/EIN/IBAN/account masks, display policy, scrub, leak-gate |
  | ZCL_MDMDOC_SNIFF | 22 | doc class/type, invoice/letter/W-8 heuristics |
  | ZCL_MDMDOC_EXTRACT | 22 | regex-overrides-LLM overlay, crosscheck, guard heuristics (officer block, e-signature) |
  | ZCL_MDMDOC_VERDICT | 19 | verdict precedence, next_step EN/RU, message_type |
  | ZCL_MDMDOC_FILE | 18 | classify_ext, .eml/.zip unwrap, sha16 |
  | ZCL_MDMDOC_COMPARE | 16 | SAP-000..009 comparison: IBAN/account/SWIFT/country/bank-key/name + masking |
  | ZCL_MDMDOC_REGEX | 13 | IBAN/SWIFT/routing/EIN/boxed-TIN extraction |
  | ZCL_MDMDOC_NORM | 13 | IBAN mod-97, to_iso2, classification, date parsing |
  | ZCL_MDMDOC_LLM | 11 | Ollama response parsing behind a test double, no network |
  | ZCL_MDMDOC_REPORT | 7 | list/JSON output, SAP COMPARISON block, masking |
  | ZCL_MDMDOC_PDF | 5 | synthetic PDFs: uncompressed stream, /Encrypt, page counter |
  | ZCL_MDMDOC_SAP_MANUAL | 3 | JSON->fields, end-to-end compare run |
  | ZCL_MDMDOC_SELFTEST | 2 | core of the pre-flight checks |
  | ZCL_MDMDOC_GOLDEN_DATA | 1 | golden parity corpus: regex→extract→rules→verdict vs the Python engine |

- ✅ **YAML-generated rule data.** Rule DATA lives in this repo (`rules/banking.yaml`,
  `rules/w9.yaml` — single source of truth); `tools/gen_rules_abap.py` (invoked by the
  Python UI's `/rules/regenerate`) compiles it into the generated class
  `ZCL_MDMDOC_RULES_DATA` in the ABAP repo. Predicate/extraction LOGIC is hand-written
  in each language and is NOT carried by the generator — that is what the parity gate
  tracks.

- ✅ **Parity gate: `tools/check_parity.py` + `PARITY.md`.** Fails loudly (exit 1) on
  any silent drift between the Python and ABAP twins. It enforces:
  1. RULE DATA parity — both repos carry semantically identical `banking.yaml`/`w9.yaml`;
  2. PREDICATE-SURFACE parity — `predicates.REGISTRY` (Python) equals the set the ABAP
     rule engine dispatches (11 predicates listed in PARITY.md, e.g. `iban_valid`,
     `swift_valid`, `tin_type_vs_classification`);
  3. YAML coverage — every `when.check` used by a rule is implemented on BOTH sides;
  4. Manifest freshness — PARITY.md lists exactly the current predicates and its
     "Pending ABAP logic ports" section is empty (currently: **empty** — the
     US numeric-IBAN guard was applied to `p_iban_valid` on 2026-07-09);
  5. **GUARD parity** — every deterministic stage_b guard has a PARITY.md entry with a
     conscious status: `ported` requires a literal `[GUARD:x]` marker in
     `zcl_mdmdoc_extract`; `n/a` documents a Python-only guard (vision/few-shot/
     provenance — see section 8); `pending` = tracked drift → non-zero exit. Currently
     9 guards `ported`, 5 `n/a`, 0 `pending`;
  6. **ONE VERSION — the `abap/` submodule pin** must equal the live ABAP checkout's
     HEAD (see `docs/SYNC.md`); a stale pin means this repo would ship an outdated
     ABAP twin;
  7. **GOLDEN parity (behavioural)** — the 11-case corpus hash
     (`tools/golden/golden_cases.json`) must equal the `GOLDEN-HASH` header baked
     into the generated `ZCL_MDMDOC_GOLDEN_DATA`, the generator hash must equal its
     `GEN-HASH` header, and a regenerate-and-diff must be byte-identical (catches
     hand edits, generator drift and a stale baked corpus);
  8. **CONSTANTS parity** — hand-duplicated constants (regexes, thresholds, phrase
     lists) registered in `tools/parity/constants.json` and marked `[CONST:id]` at
     both source sites are extracted and compared (or pinned per dialect); an
     unregistered marker fails the run.

- ✅ **Self-contained package.** No external Z-dependencies: internal deps are only the
  interface `ZIF_MDMDOC_TYPES` and the generated `ZCL_MDMDOC_RULES_DATA`. The core
  (PDF read + regex extraction + rules + verdict) works WITHOUT SAP_UI, WITHOUT Ollama,
  WITHOUT outbound HTTP.

- ✅ **Object inventory (actual `src/`):** 5 programs (`ZMDMDOC`, `ZMDMDOC_SETUP`,
  `ZMDMDOC_DOCTOR`, `ZMDMDOC_MDG_DISCOVER`, `ZMDMDOC_RULES`), 2 interfaces
  (`ZIF_MDMDOC_TYPES`, `ZIF_MDMDOC_SAP_READER`), 20 classes (13 core + 2 generated
  + 5 MDG-scenario), plus the message class `ZMDMDOC` delivered as
  `src/zmdmdoc.msag.xml` (verify it imported with the pull; SE91 fallback is
  1 message). Package `ZMDMDOC` (confirmed by `src/zmdmdoc.devc.xml`); abapGit
  metadata: MASTER_LANGUAGE=E, STARTING_FOLDER=/src/, FOLDER_LOGIC=PREFIX.
  The former INTEGRATION.md ch.2.4/ch.13.2 inventory discrepancies were fixed on
  2026-07-10 (the EN rewrite recounted both); CONTRACT.md reflects the current
  inventory.

---

## 3. Dependency checklist (standard objects + how to verify)

All dependencies are standard SAP classes, present on virtually every 7.50+ system;
verify in SE24/SE80 on stripped-down systems. Better than manual SE24 clicking:
run `ZMDMDOC_SETUP` / `ZMDMDOC_DOCTOR`, which test all of these programmatically via
the unit-tested `ZCL_MDMDOC_SELFTEST` (section 4/5).

| Object | Used in | Status | How to verify / what if absent |
|---|---|---|---|
| `/UI2/CL_JSON` | ZCL_MDMDOC_LLM, _RULES, _REPORT | ⚠️ | Component SAP_UI (standard since NW 7.40 SP08). Opens in SE24? If ABSENT: LLM calls, JSON export, JSON rules-override are disabled — the core still works. Remedy: install SAP_UI or run deterministic-only. |
| `CL_ABAP_GZIP` | ZCL_MDMDOC_PDF | ⚠️ **check first** | Inflates PDF FlateDecode (zlib RFC 1950) streams. SE24 existence is NOT enough — run `DECOMPRESS_BINARY` against a real compressed PDF on dev (top risk, section 5 item 1). |
| `CL_ABAP_ZIP` | ZCL_MDMDOC_FILE, _PDF | ⚠️ | .zip container unwrap + 3rd PDF inflation fallback. Verify existence in SE24 AND behavior on CRC mismatch (kernel releases differ in tolerance). |
| `CL_ABAP_MESSAGE_DIGEST` | ZCL_MDMDOC_FILE | ⚠️ low risk | SHA-256 → document run id. Standard since 7.40; SE24 check. |
| `CL_HTTP_CLIENT` (`CREATE_BY_URL`) | ZCL_MDMDOC_LLM | ⚠️ LLM mode only | Ollama `/api/tags` + `/api/chat`. SE24 check. Not needed for deterministic-only. |
| `CL_HTTP_UTILITY` | ZCL_MDMDOC_LLM, _FILE | ⚠️ low risk | base64 encode/decode (vision images, .eml attachments). SE24 check. |
| `CL_GUI_FRONTEND_SERVICES` | ZMDMDOC, ZCL_MDMDOC_FILE | ⚠️ low risk | PC file pick/upload, JSON download. SE24 check. Not used in batch/BAdI paths. |
| `CL_ABAP_CONV_IN_CE` / `CL_ABAP_CODEPAGE` | ZCL_MDMDOC_FILE, _PDF | ⚠️ low risk | xstring ↔ string (UTF-8/Latin-1). SE24 check. |
| `CL_ABAP_REGEX` / `FIND REGEX` | ZCL_MDMDOC_REGEX, _RULES | ✅ | Classic regex (NOT PCRE — 7.50 compat). Syntax-level; nothing to install. |

One-shot manual check (INTEGRATION.md ch.1): confirm `/UI2/CL_JSON`, `CL_ABAP_GZIP`,
`CL_ABAP_ZIP` all open in SE24.

Additional prerequisites (ch.0 TL;DR):

| Need | Status | Notes |
|---|---|---|
| ABAP >= 7.50 | ⚠️ mandatory | Check system release. |
| abapGit installed | ⚠️ mandatory | `ZABAPGIT_STANDALONE` or full version (abapgit.org). |
| Developer key / transport | ⚠️ mandatory | Unless importing into `$TMP` for local tests. |
| Role with S_GUI / S_DATASET / S_ICF | ⚠️ mandatory | See authorization table below. |
| Ollama + models (`qwen3:4b`, `qwen2.5vl:7b`) | ❌ optional | LLM mode only; not required for corp v1. |
| Outbound HTTP from the app server | ❌ optional | Only for Ollama. |

**Authorizations / operator role** (INTEGRATION.md ch.3):

| Object | Values | Purpose |
|---|---|---|
| S_TCODE | SA38 (or own Z-transaction via SE93, ch.4) | run the report |
| S_GUI | ACTVT 61 (Upload/Download) | read file from PC, download JSON |
| S_DATASET | PROGRAM `ZMDMDOC*`, ACTVT 33 (read) / 34 (write), path filter | application-server mode (OPEN DATASET) |
| S_ICF | ICF_FIELD SERVICE, per outbound-HTTP policy | Ollama calls (LLM mode ONLY) |
| S_DEVELOP | dev system only | activation / unit tests |

If LLM is unused → no S_ICF needed. If files come only from PC → S_DATASET can be
omitted (the "application server" radio button simply won't work — expected).
The MDG BAdI path needs **no extra RFC/HTTP** (LLM is off in the BAdI path;
attachment/entity reads run under the CR user's own authorizations).

---

## 4. Import runbook (abapGit)

Package: **`ZMDMDOC`**. Ignore list in `.abapgit.xml`: `/.gitignore`, `/LICENSE`,
`/README.md`, `/CHANGELOG.md`.

1. **Install abapGit** — report `ZABAPGIT_STANDALONE` or the full version.
2. **Online mode only:** set up SSL for github.com in STRUST (PSE
   "SSL client SSL Client (Standard)") and enable the service in SICF if needed.
   Offline (ZIP) mode needs no SSL.
3. **Create the target package:** SE80 → Create → Package → `ZMDMDOC` (or your
   Z-namespace); Software Component HOME/LOCAL; assign a transport layer if moving
   between systems; `$TMP` is fine for local tests (no transport).
4. **Import:**
   - Online: abapGit → New Online → repo URL → Package `ZMDMDOC` → Branch `main` → Pull.
   - Offline: zip the repo contents (`src/` folder mandatory, `.abapgit.xml` at root;
     e.g. `git archive` or the hosting's ZIP) → abapGit → New Offline → Package
     `ZMDMDOC` → Import ZIP → Pull.
5. **Activate everything:** SE80 → package `ZMDMDOC` → Activate all (Ctrl+F3).
6. **Post-import verification** (ch.2.5):
   - SE80 → select all classes → Run → Unit Tests (Ctrl+Shift+F10). All tests are
     HARMLESS/SHORT, no network/files — **all package tests must be green**
     (204 as of 2026-07-10 — recount on your system, the number grows with updates).
   - SA38 → `ZMDMDOC` → selection screen opens without syntax errors.
7. **Message class:** shipped as `src/zmdmdoc.msag.xml` (message `001` =
   `&1&2&3&4`) — verify it imported with the pull; if your abapGit build skipped
   it, create it manually: SE91 → `ZMDMDOC` → message `001`.

**Recommended activation/enablement order** (INTEGRATION.md ch.13.3):

1. Import package → activate → ABAP Unit on the package (Ctrl+Shift+F10), all green
   (204 as of 2026-07-10 — recount on your system, the number grows with updates).
2. Run **`ZMDMDOC_SETUP`** without `p_cr` — core + discovery checks green, review the
   proposed mapping. `ZMDMDOC_SETUP` is the recommended single entry point (ch.11.0):
   a ONE-SHOT onboarding report that runs, in order, (1) pre-flight tests
   ("can it load / can it read"), (2) MDG field-architecture discovery (proposed
   mapping + gaps), (3) if `p_cr` is given, a LIVE change-request read (fields +
   attachments counted), (4) if `p_save` is set, persists the proposed mapping to
   table `ZMDMDOC_MAP`. Parameters: `p_model` (default 'BP'), `p_cr` (optional),
   `p_list` (default 'X'), `p_save` (default off). All logic is delegated to
   `ZCL_MDMDOC_ONBOARD=>run(...)`; output is a single colored PASS/FAIL/SKIP report
   with a GO / NO-GO summary.
3. If needed, create/fill `ZMDMDOC_MAP` (SE11, section 5 item 10), rerun
   `ZMDMDOC_SETUP` with `p_save`.
4. Run `ZMDMDOC_SETUP` with a test `p_cr` — checks "read CR fields" and
   "read CR attachments" must be green.
5. **Only after GO** — activate the BAdI (section 6).

**Auto-mapping — `ZMDMDOC_MDG_DISCOVER`** (ch.12): discovery-only focused view
(marked `*** VERIFY ON SYSTEM ***` in its header, excluded from abaplint). Thin
wrapper over `ZCL_MDMDOC_ONBOARD=>run` without the CR read. It reads the live
data-model architecture (entities + fields, fields via RTTI), matches real field
names against a synonym list (BANKS→bank_country, BANKL→bank_key,
BANKN→bank_account, IBAN→iban, NAME_ORG1→account_holder, STREET→street,
CITY1→city, TAXNUM→tin, ...), PROPOSES the SAP_KEY→entity.field mapping, shows
uncovered keys to fill in manually, and can persist the proposal with `p_save`.
Recommended flow (ch.12.1): DISCOVER → review proposal → adjust → save →
`ZMDMDOC_DOCTOR` with a CR number (confirm fields readable) → enable BAdI.

**`ZMDMDOC_DOCTOR`** (ch.11.1): checks-only focused view (`p_model` default BP +
optional `p_cr`); the check logic lives in the unit-tested `ZCL_MDMDOC_SELFTEST`,
so the same checks are callable programmatically.

---

## 5. Verify-on-system checklist ⚠️

Every item: **what** to check, **how**, **why** it may differ, **what to adapt**.

**Core (in documented risk order, INTEGRATION.md ch.7):**

1. ⚠️ **`CL_ABAP_GZIP=>DECOMPRESS_BINARY` with zlib streams (RFC 1950)** — top risk.
   - How: run it against a REAL compressed PDF on the dev system (SE24 existence is
     not enough); or simply run `ZMDMDOC_SETUP` and feed `ZMDMDOC` a real
     `bank_letter.pdf`.
   - Why: PDF FlateDecode is zlib, not gzip; kernel behavior differs.
   - Adapt: `ZCL_MDMDOC_PDF` already tries 3 inflation strategies (direct → strip
     zlib header + synthetic gzip envelope → synthetic ZIP via `CL_ABAP_ZIP`). If all
     3 fail on the kernel, only PDFs with uncompressed streams are readable;
     documented workaround (INTEGRATION.md ch.7 — note its pointer to the README is
     stale): re-export the PDF via "print to PDF". The README itself documents
     different workarounds: convert the page to PNG and use LLM-vision, or OCR
     outside SAP.
2. ⚠️ **`/UI2/CL_JSON` present** (component SAP_UI).
   - How: SE24; or the `/UI2/CL_JSON round-trip` pre-flight check.
   - Why: not every system has SAP_UI installed / at level.
   - Adapt: absence disables LLM / JSON export / JSON rules-override; core keeps working.
3. ⚠️ **`CL_ABAP_ZIP` behavior on CRC mismatch.**
   - How: SE24 + a test .zip; pre-flight check covers availability.
   - Why: used for .zip containers and PDF inflation strategy 3; kernel releases
     differ in tolerance.
4. ⚠️ **Remaining standard classes** (`CL_ABAP_MESSAGE_DIGEST`, `CL_HTTP_CLIENT`,
   `CL_HTTP_UTILITY`, `CL_GUI_FRONTEND_SERVICES`) — standard since 7.40, low risk;
   SE24 / pre-flight checks.

**MDG-specific (ch.10.3 + ch.12.3).** Seven objects are deliberately excluded from
offline abaplint (see section 2): the MDG classes `ZCL_MDMDOC_MDG_READER`,
`ZCL_MDG_BP_FIELD_DERR_VAL`, `ZCL_MDMDOC_MDG_MAP`, `ZCL_MDMDOC_ONBOARD` and the
programs `ZMDMDOC_MDG_DISCOVER`, `ZMDMDOC_DOCTOR`, `ZMDMDOC_SETUP` — so the whole
setup/doctor/onboard onboarding path is also verify-on-system for syntax. All of
them except `zmdmdoc_setup.prog.abap` carry `*** VERIFY ON SYSTEM ***` in their
source headers:

5. ⚠️ **Signature of `IF_EX_USMD_RULE_SERVICE~CHECK_ENTITY`** — exact parameter
   names/types (`io_model` / `i_crequest` / `i_fieldname` / `ct_message`) and the
   message-return mechanism.
   - How: SE24/SE18 on the target release; compare with the implementation.
   - Why: the implementation inherits the signature from the interface, and MDG
     releases differ.
   - Adapt: fix the references in `ZCL_MDG_BP_FIELD_DERR_VAL`. Note the anchor guard
     compares `i_fieldname` against entity name `'BP_BANKDT'` — confirm that
     CHECK_ENTITY receives the entity name in that parameter on your release.
6. ⚠️ **Type of `io_model`** (`if_usmd_model_ext` vs another) and the exact
   `read_entity_data_all` / `create_data_reference` calls (structure-constant names —
   the reader uses `if_usmd_model=>gc_struct_key_attr`).
   - How: SE24 on the target release.
   - Why: MDG API surface varies by release/SP.
   - Adapt: fix calls in `ZCL_MDMDOC_MDG_READER`.
7. ⚠️ **Technical entity and field names of data model BP.**
   - How: MDGIMG → "Edit Data Model" / transaction USMD_ENTITY; or run
     `ZMDMDOC_MDG_DISCOVER` which lists them live.
   - Why: entity/field names differ between customers (custom data models, renamed
     entities).
   - Adapt: confirm/adjust the ch.10.4 default mapping:
     account_holder/account_name ← BP_CENTRL (or BP_HEADER).NAME_ORG1(+2)/
     NAME_FIRST+LAST; street/city ← ADDRESS.STREET/CITY1; bank_country ←
     BP_BANKDT.BANKS; bank_key ← BP_BANKDT.BANKL; bank_account ← BP_BANKDT.BANKN;
     control_key ← BP_BANKDT.BKONT; iban ← BP_IBAN (or BP_BANKDT).IBAN;
     bank_name/swift_bic derived from BANKS+BANKL → BNKA (active) fields BANKA/SWIFT
     (the reader's `derive_bank_master` does `SELECT SINGLE banka, swift FROM bnka`);
     tin (US) ← BP_TAXNUM.TAXTYPE (US1/US2) + TAXNUM.
8. ⚠️ **CR attachment API** — the GOS object type for the change request
   (`USMD_CREQ` in the template; marked "CONFIRM BOR/IBO object type of the CR" in
   code) and class `CL_GOS_API` (or the alternative on your release).
   - How: `ZMDMDOC_SETUP`/`ZMDMDOC_DOCTOR` with a test `p_cr` — the
     "read CR attachments" check reports the actual attachment count.
   - Why: attachment storage/API differs by MDG release.
   - Adapt: the method is written as a template with graceful fallback — an
     unavailable API returns an error string, never dumps; substitute the object
     type/API found on-system.
9. ⚠️ **Message class `ZMDMDOC` / message `001` = `&1&2&3&4`** — must be created
   manually via SE91 (not shipped via abapGit).
   - Why: message classes are system-local objects; the BAdI's emitted messages
     reference msgid 'ZMDMDOC' msgno '001'.
   - Adapt: create it; the pre-flight check "message class ZMDMDOC / 001" confirms.
10. ⚠️ **Anchor entity** — constant `c_anchor_entity = 'BP_BANKDT'` in
    `ZCL_MDG_BP_FIELD_DERR_VAL` fires the validation once per check.
    - Why: your model may name the bank-details entity differently.
    - Adapt: change the constant. (Note: INTEGRATION 10.5 step 4 also mentions
      `c_ent_*` constants in `ZCL_MDMDOC_MDG_READER` — **stale**: the current reader
      is map-driven via `ZCL_MDMDOC_MDG_MAP` / table `ZMDMDOC_MAP`; no hard-coded
      entity constants remain.)
11. ⚠️ **BAdI enrollment**: SE18/SE19 (or MDGIMG BAdI for validations/derivations) →
    enhancement spot `USMD_RULE_SERVICE` → new implementation → class
    `ZCL_MDG_BP_FIELD_DERR_VAL`, filter `USMD_MODEL = 'BP'`, activate.
    - Why: spot/filter naming should be confirmed against your MDG customizing.
12. ⚠️ **Discovery APIs** (ch.12.3): `ZMDMDOC_MDG_DISCOVER` (via
    `ZCL_MDMDOC_ONBOARD`) uses `cl_usmd_model_ext=>get_instance` / `get_entities` /
    `create_data_reference` — verify against your MDG release (marked in code).
    Field names themselves are read via RTTI on the entity structure, so only the way
    of obtaining the entity LIST may need adaptation.
13. ⚠️ **Optional table `ZMDMDOC_MAP`** (ch.12.2) — create in SE11 only if you want a
    persisted mapping: transparent customizing table, delivery class C; key fields
    MODEL (USMD_MODEL, CHAR30) + SAP_KEY (CHAR40); data fields ENTITY (USMD_ENTITY,
    CHAR30), FIELD (USMD_FIELDNAME, CHAR30). `ZCL_MDMDOC_MDG_MAP` reads it
    DYNAMICALLY (`SELECT ... FROM ('ZMDMDOC_MAP')`), so all classes activate and run
    even when the table does not exist (built-in defaults apply).

Do not invent alternatives for any of the API names above: the repo itself flags
them as verify-on-system.

**Pre-flight checks reference** (ch.13.1; run by `ZMDMDOC_SETUP` / `ZMDMDOC_DOCTOR`,
each independent, PASS/FAIL/SKIP):

- Core (unit-tested, no MDG — `ZCL_MDMDOC_SELFTEST`): CL_ABAP_GZIP available;
  CL_ABAP_ZIP available; CL_ABAP_MESSAGE_DIGEST available; CL_HTTP_CLIENT available;
  ZCL_MDMDOC_COMPARE available; ZIF_MDMDOC_SAP_READER available; /UI2/CL_JSON
  round-trip; masking (IBAN never printed in full); PDF text extraction; comparator
  (artificial mismatch → SAP-001).
- MDG (verify-on-system): IF_USMD_MODEL_EXT available; ZCL_MDMDOC_MDG_READER /
  ZCL_MDG_BP_FIELD_DERR_VAL available; CL_GOS_API available; message class
  ZMDMDOC/001 exists; MDG model read (`p_model`); read CR fields (`p_cr`);
  read CR attachments (`p_cr`).

---

## 6. MDG/Fiori flow status

Target flow: user attaches a document to a Change Request → presses the standard
**Check** → the BAdI reads CR bank data + GOS attachments → extracts + compares →
emits warnings SAP-000..008 into the CR check log (visible in Fiori
"My Change Requests" / the CR UI, ch.10.7).

**Code-complete ✅ (unit-tested locally, SAP-independent):**

- ✅ Entry point `IF_EX_USMD_RULE_SERVICE~CHECK_ENTITY` with the anchor guard
  (`IF i_fieldname <> c_anchor_entity. RETURN.` with `c_anchor_entity = 'BP_BANKDT'`)
  so the whole validation fires exactly once per check. The other interface methods
  (`check`, `check_all`, `derive`, `derive_default`, `initialize`) are implemented empty.
- ✅ `run_cr_validation` pipeline: (1) read CR attachments (silent return if none);
  (2) read CR fields (map-driven entity reads); (3) per attachment:
  `zcl_mdmdoc_file=>classify_ext` — only 'pdf' processed, others skipped (the
  in-BAdI path validates PDFs with a text layer only); extraction =
  `zcl_mdmdoc_pdf=>extract_text` → `zcl_mdmdoc_sniff` doc class + type hint →
  `zcl_mdmdoc_regex=>extract_candidates` → `zcl_mdmdoc_extract=>build` with
  `iv_llm_used = abap_false` (fully deterministic, no external HTTP — the BAdI is
  synchronous); (4) `zcl_mdmdoc_compare=>compare( ... iv_policy = 'masked' )`
  producing findings SAP-000..SAP-008; (5) each finding → `emit_finding`.
- ✅ `emit_finding` mapping: severity NOTE (SAP-000 "all matched") is SKIPPED — never
  shown; message type `E` when the finding's verdict effect is REJECT (blocks
  submit), else `W` (non-blocking warning). Text `[<rule_id>] <message>` chopped
  into 50-char chunks across MSGV1..MSGV4; row type `usmd_s_message` with
  `msgid = 'ZMDMDOC'`, `msgno = '001'`, inserted into `ct_message`
  (TYPE `usmd_ts_message`). Values masked, e.g.
  `[SAP-001] IBAN mismatch ... DE**…4931 vs DE**…4999` (ch.10.8).
- ✅ SAP-000..008 comparator semantics (CONTRACT.md, `ZCL_MDMDOC_COMPARE`, 14 unit
  tests): SAP-001 IBAN mismatch (char-by-char, first-diff position,
  CRITICAL/REVIEW); SAP-002 IBAN present on only one side (WARNING); SAP-003 account
  (zero-pad + in-IBAN tolerance, CRITICAL); SAP-004 SWIFT (±XXX branch suffix,
  CRITICAL); SAP-005 country via to_iso2 (CRITICAL); SAP-006 bank_key
  unconfirmed-by-document (WARNING); SAP-007 bank name substring (WARNING); SAP-008
  account holder substring (CRITICAL); SAP-000 all-match (NOTE, suppressed). Both
  sides masked via `ZCL_MDMDOC_MASK`; full sensitive values never leave the comparator.
- ✅ Performance design (ch.10.6): PDF parse + regex = milliseconds; execution is
  limited to the anchor entity and PDF attachments only, so every CR check is not
  slowed. No extra RFC/HTTP in the BAdI path.

**Verify-on-system ⚠️ (the SAP-facing seams — details in section 5):**

- ⚠️ CHECK_ENTITY signature / parameter names on the target MDG release (item 5).
- ⚠️ `io_model` type and `read_entity_data_all` calls in the reader (item 6).
- ⚠️ BP entity/field names + mapping, anchor entity choice (items 7, 10).
- ⚠️ GOS attachment API + CR object type `USMD_CREQ` (item 8).
- ⚠️ Message class ZMDMDOC/001 creation in SE91 (item 9).
- ⚠️ BAdI implementation creation + filter `USMD_MODEL = 'BP'` + activation (item 11).
- ⚠️ End-to-end test: CR + wrong-IBAN attachment → SAP-001 warning in the CR log
  (ch.10.9 final acceptance step).

**Not-implemented ❌ (in the BAdI path, by design):**

- ❌ LLM/vision inside the BAdI — deterministic-only; image scans and PDFs without a
  text layer are skipped in this path (see section 8).
- ❌ Persistent note to the approver — see section 7.

---

## 7. HANDOFF SPEC: `add_cr_note` (Data Owner / approver note) ❌ → on-system work

**Status: ❌ not-implemented.** There is NO persistent "note to the Data Owner /
approver" mechanism anywhere in the repo (`grep -rin add_cr_note` returns zero
hits). Today, findings exist only as transient check-log messages inside
`ct_message` — regenerated on every CR check, not stored as a CR note/comment/
annotation visible to the approver outside the check log. No API for creating CR
notes is named anywhere in the repo, so the concrete mechanism is **verify on
system**. This section is the specification for the ABAP session that will
implement it ON the target system. **Owner: the on-system ABAP session — do not
attempt to implement it off-system.**

### 7.1 Desired signature

New method on the SAP reader adapter (so all SAP-specific access stays isolated in
`ZIF_MDMDOC_SAP_READER` implementations and the comparator/types/report/self-test
remain SAP-independent and unit-tested). Add to interface `ZIF_MDMDOC_SAP_READER`,
implement in `ZCL_MDMDOC_MDG_READER`:

```abap
METHODS add_cr_note
  IMPORTING
    iv_cr          TYPE usmd_crequest   " change request number
    iv_note        TYPE string          " note text — MASKED content only
    iv_target_role TYPE string OPTIONAL " informational: intended reader,
                                        " e.g. 'DATA_OWNER' / 'APPROVER'
  EXPORTING
    ev_written     TYPE abap_bool       " abap_true only if a note was persisted
    ev_error       TYPE string.         " empty on success; degradation reason otherwise
```

(Exact typing of `iv_target_role` may be refined on-system if a real role/agent
type exists for CR notes; until then a plain string is deliberate — do not invent
an SAP domain for it.)

### 7.2 Inputs and behavior

- **Inputs:** CR number (`iv_cr`), note text (`iv_note`, already masked — see 7.4),
  optional target role for the intended reader (`iv_target_role`).
- **Behavior — graceful fallback, same pattern as the existing GOS read template:**
  1. Try the first candidate note/comment mechanism **IF it is present on the
     system** (check class/FM existence, then call inside `TRY ... CATCH cx_root`).
  2. On success: `ev_written = abap_true`, `ev_error` empty. Stop.
  3. On absence/failure: optionally try the next candidate the same way.
  4. If no mechanism works: **degrade to a check-log message** — the caller
     (`ZCL_MDG_BP_FIELD_DERR_VAL`) appends one extra `usmd_s_message` row
     (msgid 'ZMDMDOC', msgno '001', type 'W') carrying a truncated form of the
     note, and `ev_error` returns a human-readable reason, e.g.
     `|CR note API not available on this release: { lx->get_text( ) }|`.
     Never dump; never raise; never block the check.
- **Call site:** end of `run_cr_validation` in `ZCL_MDG_BP_FIELD_DERR_VAL`, after
  `emit_finding` loop — a single summary note (not one per finding), only when at
  least one non-NOTE finding was emitted. Failure of `add_cr_note` must not change
  the check result.

### 7.3 Template ingredients to copy (from the existing GOS read method)

The reference pattern is `zif_mdmdoc_sap_reader~read_cr_attachments` in
`src/zcl_mdmdoc_mdg_reader.clas.abap`:

- (a) a `*** VERIFY ON SYSTEM ***` header comment naming exactly what to confirm
  (object type + API per MDG release);
- (b) generic object key `sibflporb` with `typeid = 'USMD_CREQ'` flagged
  "CONFIRM BOR/IBO object type of the CR", `instid` = CR number, `catid = 'BO'`;
- (c) the entire release-specific API call wrapped in `TRY ... CATCH cx_root` so an
  unavailable API yields a returned `ev_error` string instead of a dump;
- (d) all SAP-specific access isolated in the reader/adapter class;
- (e) **masked-only values in anything written** — policy 'masked' is already the
  BAdI default; TINs are masked under every policy per
  `ZCL_MDMDOC_MASK.display_value`.

### 7.4 Candidate SAP mechanisms to investigate ON SYSTEM

Do NOT assert any of these exist — the repo does not name a note-creation API.
Investigate in this order and use the first that is actually present and writes
something the approver can see:

1. **The write-side of the same GOS API used for reading** — the reading side uses
   `CL_GOS_API` (`create_instance` / `get_atta_list` / `read_attachment` /
   `get_content` / `get_description`). Whether the same GOS API on the target
   release supports **creating** notes/annotations (a note-type attachment on the
   `USMD_CREQ` object) must be checked on-system — verify on system.
2. **A native MDG change-request note/comment facility** — MDG CRs display notes in
   the CR UI on some releases; whether a public API/class/FM exists to append one
   is release-dependent — verify on system (SE24/SE37 exploration around the
   USMD*/MDG* namespaces; do not hard-code a name until found).
3. **A GOS attachment as the note carrier** — attach a small text file
   ("mdmdoc-findings.txt", masked summary) via the GOS attachment write path if one
   exists on the release — verify on system.
4. **Fallback (always available):** the check-log message described in 7.2 step 4 —
   this needs no new API and is the guaranteed degradation floor.

### 7.5 Acceptance criteria for the on-system session

- Method compiles and activates with the rest of the package; abaplint exclusion
  handled the same way as the other MDG classes.
- With no working note API: check still completes, one extra 'W' log message
  appears, `ev_written = abap_false`, `ev_error` filled — no dump.
- With a working note API: approver can see the note on the CR outside the transient
  check log; note content is masked (spot-check: no full IBAN/TIN anywhere).
- Unit tests for the pure parts (note-text assembly, masking) added off-system in
  the existing local-test-class style; the API call itself is on-system only.

---

## 8. Known limitations — corp v1

- ❌ **Deterministic-only without Ollama.** The BAdI/MDG path always runs with
  `iv_llm_used = abap_false` — no LLM, no outbound HTTP, synchronous. The
  interactive `ZMDMDOC` report can optionally use an LLM.
- ❌→⚠️ **LLM is optional and requires network setup, not SM59.** Per INTEGRATION.md
  ch.5.3, plain-HTTP Ollama calls use `CL_HTTP_CLIENT=>CREATE_BY_URL` — an outbound
  call needing **no SM59 destination and no SICF service**; only network
  reachability from the APPLICATION SERVER (not the operator's PC) + S_ICF
  authorization. HTTPS additionally needs the endpoint's CA cert in STRUST. A
  corporate proxy requires a one-line edit at the single designated point in
  `ZCL_MDMDOC_LLM`'s client-creation method. Graceful degradation: with the LLM
  checkbox on, a failed 5 s `GET /api/tags` probe yields finding `LLM-001` and the
  run CONTINUES deterministic — bad HTTP config degrades, never breaks.
- ❌ **No vision path in ABAP.** Image scans (.png/.jpg) and PDFs without a text
  layer cannot be transcribed without the (optional, non-corp-v1) LLM vision model;
  in the BAdI path they are skipped / flagged. The corresponding Python-only guards
  are recorded as `n/a` in `PARITY.md` (## Guards): `apply_w9_zone_probe` and
  `apply_signature_probe` (vision probes), `drop_exemplar_echo` (few-shot exemplar
  echo — ABAP has no few-shot dataset), `finalize_provenance` (Python report
  provenance structure — the ABAP result has no provenance field). The parity gate
  keeps this list honest.
- ❌ **Deliberately NOT ported from the Python original** (README): web panel /
  REST API, teach loop (review → labels → few-shot → LoRA → adoption gate), eval
  framework, web enrichment. The ABAP clone is the validation pipeline only.
- ⚠️ **Unreadable inputs degrade to reserved findings, not failures**
  (CONTRACT.md): EXT-001 unreadable/no text layer, EXT-002 image without
  LLM/vision, EXT-003 encrypted PDF, EXT-004 partial PDF decode, EXT-005 .msg
  unsupported (all WARNING; EXT-001/002/003/005 ⇒ NEED_MANUAL_REVIEW); LLM-001 LLM
  enabled but unreachable (WARNING, NEED_MANUAL_REVIEW); LLM-002 LLM disabled by
  user (NOTE, no effect).
- ✅ **Batch/programmatic use is covered** (for completeness): background variant
  with app-server file path; "strict mode" makes REJECT raise MESSAGE type 'E' →
  job Canceled in SM37 (machine-readable analog of Python exit code 1);
  ACCEPT/WARNING → Finished. Exit-code mapping: 0 ACCEPT → Finished (MESSAGE S);
  1 REJECT → Canceled (MESSAGE E); 2 REVIEW/WARNING → Finished (MESSAGE W);
  3 LLM down → finding LLM-001, Finished; 4 unreadable → EXT-001/EXT-002, Finished.
  Programmatic call: `SUBMIT zmdmdoc ... AND RETURN` then
  `IMPORT verdict json FROM MEMORY ID 'ZMDMDOC_RESULT'`; verdict in
  {ACCEPT|REJECT|WARNING|NEED_MANUAL_REVIEW}. An optional RFC wrapper (~30 lines)
  is deliberately not shipped; `ZCL_MDMDOC_FILE.ty_doc` already accepts xstring
  content directly.
- ⚠️ **Rules maintenance after install** (ch.8): view/export in SAP via report
  `ZMDMDOC_RULES`; permanent change = edit `rules/banking.yaml` / `rules/w9.yaml`
  in the Python repo → `python3 tools/gen_rules_abap.py` → abapGit Pull → activate
  → transport; fast no-transport override via the `p_rules` JSON parameter (broken
  JSON → warning + fallback to compiled rules); per-class partial "skill" swap:
  `rules/w9.rules.json` replaces ONLY the W-9 rule set, banking stays default (and
  vice versa). Note: on the Python side the rules gate is live (enforce_approvals =
  true, `rules/approvals.json`, approval panel at `/ui/rules/approve` on the prod
  instance) — rule changes flow to ABAP only after approval there.
