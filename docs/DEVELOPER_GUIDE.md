# Developer Guide — how mdmdoc is implemented

Audience: a programmer taking over the codebase. This is the "how it all works"
map; it links to the focused docs instead of duplicating them. For the compact
component overview read [ARCHITECTURE.md](ARCHITECTURE.md) first; for the
analyst-facing view see [USER_GUIDE.md](USER_GUIDE.md).

> **Note (2026-07-09).** The "portable labeled corpus" feature built by a
> parallel session has landed: `config.py` has `CORPUS_DIR`
> (`dataset/corpus`, env `MDMDOC_CORPUS_DIR`), `dataset.py` has
> `resolve_doc_path`/`portable_doc_path`, `tools/migrate_corpus.py` exists
> and labels are migrated, and `evalrun.py` resolves label paths via
> `dataset.resolve_doc_path`. `labels.jsonl` stores `doc_path` relative to
> `dataset/corpus`, so corpus+labels rsync between machines.

## 1. What this is

`mdmdoc` is a local-first validator for SAP MDM vendor-master support
documents: (a) banking support documents, (b) US W-9/W-8 tax forms. Local LLMs
(Ollama) only classify and extract; explicit YAML rules decide verdicts —
invariant #1 of the project. One codebase serves three faces:

| Face | Entry | Consumer |
|---|---|---|
| CLI | `mdmdoc check / check-bank / check-w9 / review / train / eval / export-lora / runs / doctor / skill-rules / ui / serve` | operator, scripts |
| Operator web console | `mdmdoc ui` → `http://127.0.0.1:8766/ui` | the MDM analyst |
| REST API | `mdmdoc serve --api-only` (Docker image in `btp/`) | BTP integration |

Plus a fourth surface that is a separate repo: the **ABAP twin** `ZMDMDOC`
(see §10), pinned here as the `abap/` submodule.

CLI exit codes: `0` ACCEPT, `1` REJECT, `2` WARNING/NEED_MANUAL_REVIEW,
`3` Ollama down, `4` unreadable document.

## 2. Repo layout

```
src/mdmdoc/
  pipeline.py        run_check() — the end-to-end orchestrator
  stage_a.py         frozen perception (survey/select/deep-read/probes)
  stage_b.py         trainable extraction + the deterministic guard pack
  ocr.py             tesseract + deterministic ID regex
  fields.py          taxonomies, page scoring, crosscheck, IBAN utilities
  rules/engine.py    YAML rule engine        rules/predicates.py  predicate REGISTRY
  rule_approvals.py  human-approval hard gate (rules/approvals.json)
  rule_propose.py    propose-only rule-change flow (dispute → YAML diff)
  rules_io.py        the ONLY module that writes rules/*.yaml; ABAP regenerate
  verdict.py         precedence fold + next_step texts
  sap_compare.py     document ↔ SAP Bank Details comparison (source-agnostic)
  web_enrichment/    NOTE-only external evidence (aba/swift/entity/egress/http/match)
  privacy.py         mask/scrub/fakes/leak gate — the choke points
  report.py          report.md + machine JSON (schema mdmdoc.v1)
  runstore.py        runs/<sha16>/ artifacts, every write leak-gated
  review.py / review_core.py   the teach-loop entry (CLI + web share the core)
  scenarios.py       failure-shaped scenario tags + auto-suggestion
  training_queue.py  ranks runs worth labeling next
  fewshot.py / modelfile.py / adoption.py / evalrun.py / lora_export.py  training
  dataset.py         labels.jsonl append/erase (+ portable corpus, in flight)
  evidence.py        UI evidence crops (deterministic page+zone resolution)
  estimate.py        rolling-mean duration estimates per run shape
  skill_rules.py     read-only checker-skill parser (mdmdoc skill-rules)
  model_client.py    roles, host resolution, unload; never starts a server
  cli.py             CLI entry points + verdict → exit-code mapping
  compare.py         v2 STUB: vendor-template (.xlsx) comparer protocol
  config.py          paths, page caps, policies, CORPUS_DIR (in flight)
  server/            FastAPI app, API, jobs, UI (see §8)
rules/               banking.yaml, w9.yaml (verdict source of truth), approvals.json
prompts/fewshot/     exemplars (fake values only)  — do not edit by hand
templates/           the two report Jinja2 templates (UI templates: src/mdmdoc/server/templates/ui/)
dataset/  eval/  models/  runs/  inbox/   local data (gitignored where sensitive)
btp/                 shared Dockerfile (both deployments), compose.full.yaml, CF/Kyma yamls, openapi.json
tools/               check_parity.py, migrate_corpus.py
abap/                submodule pin of the ABAP twin (never edited from here)
tests/  scripts/     tests, install-launchagent.sh
```

## 3. The two-stage pipeline (`pipeline.run_check`)

### 3.1 Intake

Uploads are stored content-addressed as `inbox/<sha16>__<name>`
(`server/deps.py save_upload`); re-uploading the same bytes reuses the file and
the run id. `run_id` = sha256[:16] of content; artifacts overwrite the same
`runs/<sha16>/` directory on re-run. Container formats `.zip`/`.eml`/`.msg`
are unwrapped (`_resolve_container`): email attachments extracted (>10 KB
pdf/images, logos skipped), nested `.eml` inside zips expanded, every inner
document scored (page score + bank-letter marker +10 / invoice −3;
pdf > image > email) and the SINGLE best document analysed — the report notes
what was chosen and skipped (full batch mode is roadmap). `doc_class auto` →
`stage_a.sniff_doc_class` (filename regex → text-layer sniff → one cheap
tesseract of page 1; default "bank").

### 3.2 Stage A — frozen perception (`stage_a.perceive`)

Stage A is built to SEARCH a document, not read page 1. It is never trained.

- **Survey**: up to `SCAN_PAGE_CAP=12` pages. Text-layer PDFs are scored
  directly; scanned PDFs get a 120-DPI render + quick tesseract (CJK retry
  when <8 real words) + rotation fix — tesseract OSD `--psm 0` (applied at
  confidence ≥2.0) with brute-force 90/180/270 fallback when a page reads as
  noise.
- **Select**: `fields.page_score` = multilingual banking-keyword hits
  (EN/ES/DE/FR/PT/RU/KO/JA/ZH) + 3×regex-hit count + W-9 boxed-TIN bonus +5 +
  bank-letter page bonus +8 / invoice −2. Top `MAX_PAGES` win (bank: 2,
  W-9: 3).
- **Deep read** (winners only): 300-DPI preprocessed grayscale tesseract +
  170-DPI color renders (max side 1600) to the VISION model (qwen2.5vl:7b)
  with a transcribe-only prompt (temperature 0, seed 7). Vision only
  TRANSCRIBES in Stage A — that split is what makes Stage B trainable.
- **Packet markers** (bank only): `fields.page_markers` flags bank-letter
  pages (≥2 confirmation phrases; IT/ES/DE wording included) and invoice pages
  → drives packet-aware classification in Stage B.
- **Deterministic regex IDs** over the merged text (`ocr.regex_fields`): IBAN,
  SWIFT, EIN (`dd-ddddddd`), SSN (masked at capture), `routing_aba` vs
  `routing_aba_wires` (label-window anchored ACH/wires split),
  `account_number`; `fields.find_boxed_tin` rescues W-9 per-digit-box TINs and
  settles EIN vs SSN from the nearest preceding box label.
- **Escalation**: a bank document with no iban/account/routing after the first
  pass triggers a second TARGETED vision pass hunting payment details.
- **Vision probes** (run while VISION is resident, then `mc.unload("VISION")`):
  a signature probe on the likeliest signature page (ALWAYS for bank/W-9, even
  text-layer PDFs — wet signatures are pixels; distinguishes handwritten vs
  stamp vs DocuSign-electronic), and W-9 zone probes — upscaled (≥1100 px)
  crops of the IRS classification checkbox row and the Part I TIN boxes. Never
  applied to W-8 (different layout).
- **Special inputs**: password-protected PDF → `UnreadableDocument`
  (HTTP 422 / exit 4); editable formats (.docx/.xlsx/.txt/.rtf…) →
  `editable_source`; .eml/.msg without attachments → `email`; type hints for
  printed emails, invoices, voided checks, statements, bank letters (incl.
  开户许可证), AP documents, payment instructions.
- **Persistence** (`to_public`): raw text only as a scrubbed excerpt (1600
  chars), regex candidates masked per policy, W-9 probe minus TIN digits. Full
  text and values stay in memory only.

### 3.3 Stage B — trainable extraction (`stage_b.extract`)

Two tiers:

- **FAST** = role TEXT, default the custom Ollama model **`mdmdoc-extract`**
  (system prompt + operator few-shot exemplars baked as MESSAGE pairs; falls
  back to stock `qwen3:4b` until built). Stock models get few-shot injected at
  runtime from `prompts/fewshot/*.json`. The prompt = exemplars + filename +
  type hint + packet signals + OCR-VERIFIED CANDIDATES (regex — "trust these
  over your own reading") + document text (8000-char cap) + a strict JSON
  contract `{doc_type ∈ taxonomy, fields{...}}`; `format=json`, temperature 0,
  seed 7, `num_ctx` 16384, `think=false`. The prompt carries FULL sensitive
  values → it is never persisted.
- **STRONG** = role TEXT_STRONG (`qwen3:14b`), invoked only when it resolves
  to a distinct model and one of the escalation reasons fires (pure,
  unit-testable list): `quality-requested`, `json-retry`,
  `us-bank-no-routing`, `bank-no-account-id`, `bank-no-holder`,
  `bank-no-bank-name`, `bank-type-unclear`, `w9-no-tin`,
  `w9-no-classification`, `w9-no-line1`, `crosscheck-mismatch`.
  `_merge_tiers`: strong fills gaps and wins disagreements (with a "tier
  disagreement" note) but NEVER blanks a non-empty fast value ("absence is not
  evidence"); guards, TIN normalizer, crosscheck and audits re-run on the
  merge; strong `doc_type` is accepted only when not overridden by
  deterministic packet/type facts.

Field taxonomies (`fields.py`): BANK_DOC_TYPES = `bank_letter`,
`bank_statement`, `supplier_letterhead`, `bank_screenshot`, `voided_check`,
`payment_instructions`, `ap_document`, `invoice`, `email`, `editable_source`,
`other`; W9 = `w9`, `w8`, `other_tax`, `unknown`. BANK_KEYS cover holder,
type, bank name/country/address, iban, swift_bic, account_number,
routing_aba(+wires), branch_code, currency, doc_date, signed +
signature_evidence, partial_capture; W9_KEYS cover Lines 1–3, tin_type,
tin_raw, address, signed, sign_date.

Packet-aware `doc_type` overrides (deterministic beats model): a bank-letter
page in the packet beats "invoice"; a genuine invoice page with NO bank letter
forces `invoice` (REJECT via BNK-001) even when the deep-read pages were
payment instructions; a type-hint "invoice" overrides a hesitant model.

#### The deterministic guard pack (parity-tracked)

Module-level `_guard(ext[, raw])` functions in `stage_b.py`. Each is
hand-ported to ABAP with a `[GUARD:<name>]` receipt (or explicitly `n/a`) —
see §10 and [SYNC.md](SYNC.md).

| Guard | Job |
|---|---|
| `_drop_exemplar_echo` | drops values the model copied from a few-shot fake that is not present in the document text; exemplars built from this same doc sha are skipped. *(n/a in ABAP — no few-shot there)* |
| `_drop_filename_echo` | drops names lifted from the filename rather than the document |
| `_drop_regulator_noise` | drops bank_name that is really a regulator watermark (e.g. Colombian "VIGILADO Superintendencia") |
| `_normalize_tin` | canonical SSN/EIN forms; a date can never be a TIN — a date found there is rescued into `sign_date` |
| `_audit_bank_ids` | non-IBAN-shaped value in the IBAN field relocated to `account_number` (the US "no IBAN" case — never fires BNK-011); mod-97 repair of a model-garbled IBAN from checksum-valid candidates in the raw text (`fields.find_valid_ibans`, NFKC full-width tolerant) with the checksum stated as an audit fact; Italian ABI/CAB codes moved out of routing fields and verified against IBAN structure (CAB → `branch_code`); a printed account number beats a zero-padded IBAN fragment |
| `_fix_jp_form` | Japanese bank forms: 〒 postal code is not an account; labeled 口座番号/支店 rescue; NFKC full-width digits; split-stream heuristic; `bank_country=JP` inferred |
| `_fix_statement_period` | a statement period parsed as the document date |
| `_esignature_guard` | DocuSign/Adobe-Sign envelope = electronic signature, its timestamp = date; re-applied AFTER the vision probe, because pixels alone would report "typed name ≠ signature" |

#### Cross-check, probes, provenance

- `fields.crosscheck_ids`: regex outranks the model on IDs — fills blanks
  ("filled-from-OCR(masked)"), confirms matches (incl. zero-padded account
  variants), flags `MISMATCH(model=… vs ocr=…)`. Class-scoped: bank checks all
  ID fields, W-9 only the TIN (the EIN detector and boxed-TIN also SETTLE
  `tin_type`). Runs before escalation; guards re-run after each crosscheck
  because crosscheck REPLACES the notes list.
- Probes are applied last: `_apply_w9_zone_probe` (visual checkbox + TIN-box
  digits SETTLE classification/TIN over text guesses; provenance
  "zone-probe"), `_apply_signature_probe` (vision verdict outranks both text
  tiers on `signed`; a stamp counts for bank letters, not for W-9; the
  signature date is rescued), then the e-signature guard wins back.
- `_finalize_provenance`: every non-empty field gets
  `{source: model|ocr-regex|vision-crop|zone-probe|rule|precedent, page}` —
  the page attributed by per-page text search.
- Extraction registers all sensitive values + regex candidates into a per-run
  `SecretVault` (masked/fake pairs) so scrub and the leak gate know them.

## 4. Rules engine, approvals gate, propose flow

### 4.1 Engine and YAML schema (`rules/engine.py`, `rules/*.yaml`)

Declarative `when` vocabulary: `always | field_missing | flag_true |
flag_false | equals | in | regex_mismatch | check:<predicate>+field+args`,
plus `tables` (e.g. `iban_length` per country; `"NO"` must stay quoted —
YAML 1.1). Each firing rule produces a
`Finding{rule_id, severity CRITICAL/WARNING/NOTE, verdict_effect
REJECT/NEED_MANUAL_REVIEW/WARNING/ACCEPT/null, message (EN/RU with
{value}/{value_masked}/{detail} placeholders, masked per display policy),
field}`. The engine never crashes on a bad rule — it emits an `engine_error`
NOTE.

Predicates (`predicates.py` REGISTRY; contract
`predicate(value, fields, args, tables) -> (fired, detail)`, never raise):
`unsigned_no_evidence`, `unsigned_typed_block`, `field_empty`, `no_bank_ids`,
`swift_valid`, `iban_valid` (a purely numeric value in the IBAN field is a
plain account number — no fire), `ein_shape`, `tin_type_vs_classification`,
`individual_with_business_name_and_ein`, `line_swap_suspect`,
`date_older_than` (with ES/DE month parsing).

Rule inventory (severity → verdict effect): BNK-001 invoice = REJECT,
BNK-002 email = REJECT (an OPEN rule-owner decision — fork #1 in
docs/RULES_AUDIT.md recommends downgrading to NMR), BNK-003 editable file =
REJECT — **the only three auto-REJECTs**; BNK-004 payment_instructions WARNING; BNK-005 ap_form NOTE;
BNK-006 statement-no-SWIFT NOTE; BNK-010 SWIFT shape CRITICAL→NMR; BNK-011
IBAN length/checksum/country CRITICAL→NMR; BNK-020 older-than-2y NOTE;
BNK-021 unsigned letter WARNING; BNK-026 typed officer block NOTE; BNK-022
partial screenshot →NMR; BNK-023 no holder →NMR; BNK-024 no bank IDs →NMR;
BNK-025 no bank name WARNING. W9-030 W-8 →NMR; W9-031 unknown tax doc →NMR;
W9-001/002/003 missing Line1/TIN/classification →NMR; W9-010 TIN≠9 digits
→NMR; W9-011 TIN-type vs classification →NMR; W9-012
individual+business+EIN →NMR; W9-013 line swap →NMR; W9-020 unsigned
WARNING. W-9 never hard-REJECTs by design. Decision context per rule:
[RULES_AUDIT.md](RULES_AUDIT.md) (RU).

> **Known discrepancy**: `rules/banking.yaml`'s `doc_types:` list omits
> `bank_statement` and `payment_instructions`, although rules use them in
> `applies_to` and `fields.py` BANK_DOC_TYPES includes them.
> `rule_propose.validate_rule` would flag `applies_to: [bank_statement]` as
> unknown. Fix the YAML list when touching this area.

### 4.2 Approvals HARD GATE (`rule_approvals.py`)

`enforce_approvals=True` in the live pipeline (`MDMDOC_RULE_GATE=1` is the
default env switch in `server/api.py _run_pipeline`; eval and tests pass
`False` to measure the raw rules). A rule fires ONLY if a human approved it in
the panel:

- **approved** = decision "approved" AND the rule's content hash (sha256 of
  the sorted-JSON rule block, 16 hex) still matches → **any edit auto-reverts
  the rule to pending**;
- **rejected** = silently skipped forever;
- **pending** applicable rule does NOT fire but injects the finding
  `RULE-GATE` (WARNING, verdict_effect NEED_MANUAL_REVIEW): "N rule(s) that
  apply … await your approval" — so nothing silently ACCEPTs.

Store: `rules/approvals.json` (rule ids + hashes + notes, no PII). It is NOT
copied to ABAP and is excluded from the deploy rsync, so the mini's live
decisions survive deployments. Panel: `/ui/rules/approve` (Approve ✓ /
Reject ✗ / Correct ✎ → editor → hash changes → back to pending; bulk
"Approve all pending").

### 4.3 Rule writing and the propose flow

`rules_io.py` is the ONLY rule writer (`save_rules`: YAML parse + rules-list +
duplicate-id validation; `test_no_writes_outside_choke_points` enforces the
named write modules). `regenerate_abap()` copies `rules/*.yaml` into the ABAP
repo (`MDMDOC_ABAP_HOME`, default `~/Projects/mdm-doc-validator-abap`) and
runs that repo's `tools/gen_rules_abap.py`.

Console pages: `/ui/rules` — YAML editor per doc class (save → POST
`/api/v1/rules/{doc_class}/raw`; "Regenerate for SAP" → POST
`/api/v1/rules/regenerate`; delete a rule by removing its block; replace a
whole "skill" by pasting a new file). `/ui/rules/approve` — the approvals
panel (§4.2).

**Propose-only rule changes** (`rule_propose.py` + POST
`/api/v1/runs/{id}/propose-fix`; run page "dispute" button per finding + a
free-text propose box): operator feedback → TEXT_STRONG classifies the kind:
`rule` (verdict wrong → proposed YAML edit), `extraction` (route to the
Correct/teach flow), or `needs_code` (a new predicate is needed → escalate:
Python predicate + hand-port to ABAP). For "rule": the model emits
edit/add/remove + a rule block, spliced TEXTUALLY into the YAML (deterministic
block-span replace preserving comments), validated
(severity/verdict_effect/when-op/predicate-exists/applies_to-known/no long
digit runs in messages/no duplicate ids) → a unified diff is shown; applying
goes through `rules_io.save_rules` (and then re-approval, because the hash
changed). The module never writes on its own and never persists the feedback.
Runs as a job under `PIPELINE_LOCK`.

Planned metadata (see [RULES_AUDIT.md](RULES_AUDIT.md) and PARITY.md
coordination requests): per-rule `tier: corp|experimental|learned` and
`source: skill|policy|operator`; `save_rules` must preserve unknown YAML keys
round-trip and `gen_rules_abap` treats them as additive/optional.

### 4.4 Verdict (`verdict.py`)

`decide()` folds the findings' `verdict_effect` with precedence
**REJECT > NEED_MANUAL_REVIEW > WARNING > ACCEPT**, and attaches a
`next_step` text per class/verdict (e.g. bank REJECT: "kick back — request a
bank letter / bank statement / supplier letterhead"; W-9 ACCEPT: "Line 1 →
Name 1, Line 2 → Name 2, TIN per type"). HTTP status is always 200 — the
verdict is payload; the CLI maps verdicts to exit codes.

**Operator precedent**: a confirmed label for THIS content hash overrides the
machine verdict/doc_type immediately (finding `OPERATOR-1` shows machine vs
precedent; provenance source "precedent"). Eval sets `apply_precedent=False` —
metrics measure the machine, not stored answers.

## 5. Privacy and egress invariants

Full treatment: [PRIVACY.md](PRIVACY.md). The enforced shape:

- **Single choke points**: `mask()`/`display_value()` (display),
  `scrub_text()` (free text, incl. spaced and one-digit-per-line W-9 TIN
  variants), `assert_no_leak()` (every persisted byte via `runstore.write`,
  `dataset.append_label`, adoption state, fewshot, LoRA export — it RAISES: a
  leaking write crashes instead of leaking), `egress.assert_safe_outbound`
  (network — the outbound mirror of the leak gate).
- **Two-policy model**: banking values follow `MDMDOC_BANK_VALUES` and tax
  numbers follow `MDMDOC_TIN_VALUES`; both default to full in the local operator
  console and masked in api-only/BTP. `config.gate_policy()` then blocks exactly
  the families still masked (`strict` → `tin-only` → `none`), so a revealed value
  can never trip the gate on its own content. What is displayed is exactly what
  is persisted/copied — there is no unmasked side channel.
  Two seams keep the reveal from spreading: `display_value`/`to_public` mask
  whenever the *caller* passes `policy="masked"` by name (that is how
  `reasoning.md` stays TIN-free), and training data, egress and the BTP image
  never consult the display policy at all. Locked by `tests/test_tin_reveal.py`.
  The ABAP twin has no operator console and always masks — that divergence is
  intentional, not drift.
- **Training data is ALWAYS strict**: labels are re-masked in
  `review_core.build_label`; exemplars carry only shape-preserving fakes.
- **SecretVault per run**: kind/value/masked/fake (`fake_preserve_shape` =
  same shape, different digits); the `sensitive_map` (masked↔fake pairs, no
  real values) is persisted into labels for few-shot/LoRA; fakes are
  allow-listed at the gate, real values are still caught by the known-secret
  pass which runs first.
- **Pixels**: page renders deleted per run; UI page previews and evidence
  crops render on demand into temp dirs, streamed `Cache-Control: no-store`,
  never persisted; both endpoints live on the teach router only (absent in
  the BTP image).
- **Eval leak sweep** hard-fails (exit 1) if `leakage_count > 0` over `runs/`
  (per display policy), `dataset/`, `prompts/fewshot`, `eval/` (strict).
- **Erasure**: delete `inbox/<sha16>__*`, `runs/<sha16>/`, and the label line
  by `doc_sha256`.

## 6. Web evidence layer (NOTE-only)

Full treatment: [WEB_EVIDENCE.md](WEB_EVIDENCE.md). Summary of the contract:

- Runs AFTER `decide()`, opt-in (`MDMDOC_WEB_EVIDENCE=1`, per-run
  `--web-evidence`, or the console "Verify externally 🌐" button — the click
  is the opt-in, `force=True`). Refused with 400 in api-only mode; forced off
  in eval.
- Providers: `aba` (offline ABA mod-10 checksum; FDIC BankFind by bank NAME;
  Fed routing directory only via the `MDMDOC_FED_ROUTING_URL` connector),
  `swift` (offline ISO-9362 syntax + BIC-country vs document country; SWIFTRef
  only via `MDMDOC_SWIFT_LOOKUP_URL`), `entity` (GLEIF + SEC EDGAR,
  organisation names only — the `_is_org_name` gate; personal names never
  leave the machine).
- Evidence statuses `found/conflict/not_found/unavailable` + trust tiers 1–3;
  only FOUND/CONFLICT become findings; `Evidence.to_finding()` hard-codes
  `severity=NOTE, verdict_effect=None` and `gather()` re-filters — it is
  **structurally impossible** for the web to move a verdict. The UI shows a
  permanent banner: "Web did not decide this verdict".
- Egress choke point `egress.assert_safe_outbound`: forbidden = IBAN,
  account_number and all TIN kinds (vault exact values + strict generic
  patterns; the URL-decoded form is also scanned). Allowed egress =
  routing/ABA, SWIFT/BIC, bank/company names.
- All HTTP via `http.get_json` (`trust_env=False`, 6 s timeout, descriptive
  UA for SEC, 24 h in-memory TTL cache, `None` on any failure →
  'unavailable'). `match.py` is the one strict name matcher (substring or ≥2
  meaningful tokens) shared by all connectors.

## 7. SAP compare (`sap_compare.py`)

Bank documents only, optional (`--sap` / `sap_file` screenshot). Vision reads
the SAP MDG/Fiori Bank Details screen (SAP_KEYS incl. `bank_details_id`,
`bank_account`, `iban`, `control_key`, `bank_key`, `swift_bic`, …; screenshot
downscaled to `VISION_MAX_SIDE` — raw 2× Retina overflowed the Ollama
context). Then a deterministic char-by-char compare → rows + findings:
SAP-001 IBAN mismatch (first-diff position, CRITICAL→NMR), SAP-002 IBAN
one-side, SAP-003 account (leading-zero + contained-in-IBAN tolerance,
CRITICAL), SAP-004 SWIFT (±XXX head-office suffix), SAP-005 country, SAP-006
bank key must be confirmed by DOCUMENT-side data, SAP-007 bank name
(normalized containment), SAP-008 holder (CRITICAL), SAP-000 all-match NOTE.

`compare()` is source-agnostic — in a future BTP integration, live MDG data
replaces the vision-read dict. A vision failure degrades to a warning with the
`LAST_VISION_ERROR` detail; the comparison is skipped, never fatal. The ABAP
twin implements the same SAP-000..008 logic in `ZCL_MDMDOC_COMPARE` reading CR
fields directly (§10).

## 8. Server & UI map (`src/mdmdoc/server/`)

- **app.py** `create_app(mode)`: **full** (default; UI + full API; binds
  127.0.0.1; Host-header check) vs **api-only** (core API only; teach/UI
  routes NOT registered → honest OpenAPI). `/health` is unauthenticated
  liveness (never probes the model host; Docker HEALTHCHECK). Exception
  handlers scrub every error message. UI middleware drops an httponly
  samesite=strict `mdmdoc_token` cookie on /ui page loads so `<img>`/download
  sub-resources authenticate.
- **deps.py**: `require_token` — bearer when `MDMDOC_API_TOKEN` is set
  (constant-time compare; cookie and `?token=` fallback accepted). Full mode
  rejects non-local Host headers except those in `MDMDOC_ALLOWED_HOSTS`
  (comma-separated) — this is how the tailnet name is allowed behind
  `tailscale serve` in production.
- **jobs.py**: in-process daemon-thread job registry, `/api/v1/jobs` polling
  with an `after` cursor; `PIPELINE_LOCK` serializes all
  pipeline/eval/adoption/propose model work; a thread-routing stdout proxy
  captures engine `print()` into job logs; a 500-line log ring feeds the
  debug page.
- **api.py** (`/api/v1`, all token-guarded). Core (present in the BTP image):
  `GET doctor`, `GET rules` + `rules/raw`, `POST check` (multipart:
  `file|rerun_run_id`, `doc_class bank|w9|auto`, `lang en|ru`, `use_vision`,
  `wait` sync/async, `sap_file`, `quality`, `web`; `rerun_run_id` enables
  "compare with SAP after the fact" — full values exist only in memory during
  a run, so a fresh comparison = a fresh run), `GET runs / runs/{id} /
  runs/{id}/artifacts/{name}` (strict allowlist incl. `web_evidence.json`),
  `GET jobs`. Teach-only (full mode): `preview/{page}?src=doc|sap`,
  `evidence/{key}` (crops: w9_class, w9_tin, signature, iban, swift_bic,
  account_number, routing_aba, routing_aba_wires — `evidence.py` resolves
  page+zone deterministically, renders to a temp dir, streams), review
  GET/label POST (+retrain job), propose-fix POST, labels GET,
  train/fewshot, train/modelfile, train/candidate, train/adopt,
  train/rollback, train/adoption, train/export-lora, eval POST (409
  `job_conflict` when an eval/check runs) + eval/history + eval/report, rules
  raw save / regenerate / approve. See [API.md](API.md) (note: it lags the
  code on `doc_class=auto`, the teach endpoints and some params).
- **ui.py + server/templates/ui/**: server-rendered Jinja2 + vanilla JS, no build
  step ("Codex look"; asset cache-busting by mtime). Pages: `/ui` Dashboard
  (doctor status, drag-drop checks with live job progress + duration estimate
  from `estimate.py`, optional SAP screenshot, runs list, active jobs);
  `/ui/runs/{id}` Run page (document preview, EXTRACTED DATA rows with
  per-field provenance tags + evidence-crop thumbnails, findings with
  dispute buttons, report, SAP comparison table, external-evidence panel +
  permanent web banner, operator-precedent panel, learning trace when
  labeled, artifact downloads, "Verify externally 🌐", propose-fix box);
  `/ui/runs/{id}/review` Correct form; `/ui/training` Training;
  `/ui/rules` YAML editor; `/ui/rules/approve` approvals panel; `/ui/debug`
  doctor JSON, jobs, dir sizes, log ring.

## 9. Teach loop and training

Full treatment: [../TRAINING.md](../TRAINING.md). The closed loop as
implemented:

1. **Entry points**: CLI `mdmdoc review last --open` (`review.py` wraps
   `review_core`) or the run page → "Correct — teach the model".
   `review_core.review_defaults` supplies field keys, current display values,
   taxonomies, scenario-tag options with auto-suggestions, and error_source
   options.
2. **Submission**: per-field `{action: keep|set|clear, value}` +
   doc_type_gold, verdict_gold, notes, scenarios[], error_source. Full
   sensitive values are allowed in "set" — memory only; the label stores
   masked values + derived facts (present/masked/digits/length/country/
   hyphenated) + shape-preserving fakes; `model_predicted.fields_diff` is
   masked on both sides. `dataset.append_label` replaces any earlier label
   for the same doc (latest correction wins) and is leak-gated.
3. **Scenario taxonomy** (`scenarios.py`): failure-shaped tags
   (bank_invoice_plus_letter, bank_invoice_only, bank_typed_officer_block,
   bank_image_only, bank_rotated_photo, bank_multi_aba, bank_sap_compare,
   bank_supplier_letterhead, bank_email_support; w9_image_only,
   w9_checkbox_error, w9_boxed_tin, w9_line_swap, w9_unsigned,
   w9_rotated_photo, w9_handwritten, w8_form) — auto-suggested from run
   artifacts, free-form allowed (snake_case normalized). ERROR_SOURCES route
   the fix: `ocr_missed`→Stage A, `model_mapped_wrong`→Stage B/few-shot,
   `rule_wrong`→rules/*.yaml, `doc_type_wrong`, `workbook_mismatch`.
4. **Retrain-on-label**: POST `/api/v1/runs/{id}/label` with `retrain=true`
   (default) saves the label, then a background job: (a) `build_fewshot(k=2)`
   — greedy scenario-COVERAGE selection (each pick adds unseen scenario
   units; ties broken by teaching value = fields_diff count + doc_type miss +
   completeness; exemplars use fakes, never masks — masks would teach the
   model to output masks; excerpts ≤800 chars; `doc_sha256` recorded so the
   echo guard skips same-doc exemplars); (b) builds the CANDIDATE model on
   the model host (production untouched); (c) re-runs the document — the
   precedent applies instantly and the run page renders a "learning trace"
   proving field-by-field before→corrected→now.
5. **Adoption gate** (`adoption.py`) — a retrain NEVER silently becomes
   production: build `mdmdoc-extract-candidate` (`modelfile.py`: FROM stock
   `qwen3:4b` base always — never self-stack; SYSTEM = bank+W-9 prompts;
   MESSAGE few-shot pairs; `ollama create` against the resolved host, never
   starts a server) → gated eval with TEXT=candidate (`run_eval
   record=False` → `candidate_results.json`; `history.jsonl` NOT touched —
   gate evals must never masquerade as the adopted model's track record) →
   `gate_check`: leakage==0 AND invoice_false_accept_rate==0 AND no
   regression on CRITICAL_FIELDS (bank.iban, bank.account_number,
   bank.swift_bic, w9.tin, w9.line3_classification) vs the adopted baseline →
   operator clicks **Adopt** (`ollama cp` candidate → `mdmdoc-extract`, so
   the evaluated weights are exactly production; the previous Modelfile is
   kept as the rollback target) or **Rollback** (`ollama create` from
   `Modelfile.mdmdoc-extract.previous`). State: `models/adoption.json`
   (leak-gated). Endpoints: POST `/api/v1/train/candidate`, `/train/adopt`,
   `/train/rollback`, GET `/train/adoption`.
6. **Eval** (`evalrun.py`): re-runs the FULL pipeline per label (no cached
   raw text by design); precedents OFF, web OFF, approvals OFF; scores
   doc_type/verdict/json-first-try/per-field exact match (masked tails),
   invoice_false_accept_rate, scenario slices, confusion matrix, leak sweep;
   writes `eval/last_results.json` + `history.jsonl` + `report.md` with a
   delta vs the previous run; `--scenario` filter; per-doc diff
   improved/regressed/unchanged_wrong.
7. **Training page** (`/ui/training`): labels by class, eval-history
   sparklines (doc_type_accuracy, verdict_accuracy, json_valid_first_try,
   leakage_count), last-eval failures with open/review links,
   improved/regressed/unchanged-wrong diff, per-field metrics with delta,
   scenario slices, the training queue (`training_queue.py` ranks unlabeled
   runs by teaching value: NMR/WARNING verdicts, strong-tier escalations,
   model-vs-evidence conflicts, uncovered scenarios; plus LABELED runs that
   regressed = stale gold), recommendations, the adoption panel, and the
   LoRA gate at 100 labels.
8. **LoRA ladder** (`lora_export.py`, [../TRAINING.md](../TRAINING.md)):
   gated at 100 confirmed labels (`--force` only to validate the format);
   stratified split by (doc_class, doc_type); mlx-lm chat format, fakes only,
   per-line leak gate; then `mlx_lm.lora` on Qwen/Qwen3-4B → fuse → GGUF →
   `ollama create` → ALWAYS eval before adopting.
9. **Skill sync** ([SKILL_SYNC.md](SKILL_SYNC.md), `skill_rules.py`, CLI
   `mdmdoc skill-rules <skill>`): the SAP checker skills (`mdm-w9-checker`,
   `mdm-banking-checker` at `~/.claude/skills/<skill>/references/
   dynamic_rules.md`) are the human source of truth. A deterministic
   DR-entry parser buckets active skill rules into **mechanized** (already a
   W9-/BNK- rule — curated COVERAGE dict), **advisory/needs-SAP-context**
   (routed to the ABAP MDG BAdI which sees CR fields, or kept advisory), and
   **to-review** (promote by hand). There is deliberately no automatic
   prose→predicate import — that would let the model define verdicts. The
   module is read-only; writes only via `rules_io`.

## 10. The ABAP twin (`ZMDMDOC`)

The ABAP repo `/Users/egor/Projects/mdm-doc-validator-abap` (read-only from
this repo's perspective; pinned as the `abap/` submodule) is the deterministic
validator that runs inside S/4HANA/MDG. Its own docs are authoritative:
`abap/docs/CONTRACT.md` (the shared contract), `abap/docs/INTEGRATION.md`
(13 chapters incl. the MDG BAdI flow), `abap/docs/RULES.md`, `abap/README.md`.
The one-version mechanics live in [SYNC.md](SYNC.md) and
[../PARITY.md](../PARITY.md); readiness status per item in
[SAP_READINESS.md](SAP_READINESS.md).

- **Shape**: classic Z-report `ZMDMDOC` (SA38 or an own SE93 transaction),
  abapGit import, package ZMDMDOC: 1 report + interface `ZIF_MDMDOC_TYPES` +
  12 classes (FILE, PDF, SNIFF, REGEX, LLM, EXTRACT, RULES, RULES_DATA
  (generated), VERDICT, MASK, NORM, REPORT) + COMPARE/MDG classes and the
  `ZMDMDOC_RULES` / `ZMDMDOC_DOCTOR` / `ZMDMDOC_MDG_DISCOVER` /
  `ZMDMDOC_SETUP` reports. Target ABAP ≥ 7.50, classic regex only (no PCRE);
  abaplint 0 errors (MDG classes excluded as verify-on-system); 201 unit
  tests. `/UI2/CL_JSON` optional — without it there is no LLM/JSON/override,
  but core regex+rules+verdict still works.
- **Interop contract**: the same `mdmdoc.v1` result format — `ZMDMDOC`
  exports JSON (`cb_json`) that opens on this console's run pages.
- **PDF handling**: pure-ABAP text-layer extraction (FlateDecode via
  `CL_ABAP_GZIP` with three inflation fallbacks — the #1 verify-on-system
  risk); encrypted → EXT-003; scans unreadable without LLM vision →
  EXT-001/002; `.msg` unsupported → EXT-005. Optional Ollama client
  (`CL_HTTP_CLIENT` from the APP SERVER, not the SAPGUI PC); any LLM failure
  degrades to deterministic regex-only with LLM-001/LLM-002 findings — never
  breaks the check.
- **Generated rule data**: rule DATA auto-syncs from this repo's
  `rules/*.yaml` (source of truth) via the panel's "Regenerate for SAP" or
  the ABAP repo's `tools/gen_rules_abap.py` → generated class
  `ZCL_MDMDOC_RULES_DATA` + a `rules/rules.json` runtime override. The
  generator is deterministic (byte-identical output), fails on
  ABAP-7.50-unportable regex constructs (`(?i)`, `(?s)`, `(?m)`, lookarounds,
  `\b`) and on the YAML `"NO"` pitfall, and also emits per-class partial
  override packs `banking.rules.json` / `w9.rules.json` ("skill-swap" one doc
  class without touching the other). Note: `gen_rules_abap.py` lives in the
  ABAP repo's `tools/`; this repo's `tools/` has only `check_parity.py` and
  `migrate_corpus.py`. Approvals (`rules/approvals.json`) are deliberately
  NOT copied.
- **Hand-ported logic with receipts**: predicate bodies (`predicates.py`
  REGISTRY ↔ `zcl_mdmdoc_rules` WHEN-dispatch) and the Stage-B guard pack
  (↔ `[GUARD:<name>]` markers in `zcl_mdmdoc_extract`). PARITY.md is the
  manifest; current state: 11 predicates on both sides; guards ported:
  audit_bank_ids, fix_jp_form, fix_statement_period, esignature_guard,
  drop_regulator_noise, drop_filename_echo, normalize_tin; n/a by design:
  drop_exemplar_echo (no few-shot in ABAP), the vision probes, and
  finalize_provenance; pending: none (the US numeric-IBAN port closed
  2026-07-09).
- **`tools/check_parity.py`** fails loudly on: rule-data semantic diff
  between the repos, predicate-surface diff, YAML-used checks missing on
  either side, PARITY.md manifest staleness, any "Pending ABAP logic ports"
  line, a guard without a manifest entry / a `ported` status without an ABAP
  marker / any `pending` status, and an `abap/` submodule pin ≠ the live ABAP
  HEAD (the "one version" pin; resolution order `MDMDOC_ABAP_HOME` → sibling
  checkout → submodule).
- **Consciously NOT ported**: web panel/REST, teach loop, eval framework,
  web enrichment.
- **Fiori/MDG end-user flow** (`abap/docs/INTEGRATION.md` ch. 10–13): the
  end user creates a vendor/customer Change Request in Fiori (MDG model BP)
  and attaches the bank letter / W-9. A BAdI `USMD_RULE_SERVICE`
  implementation `ZCL_MDG_BP_FIELD_DERR_VAL` (filter `USMD_MODEL='BP'`,
  anchor entity BP_BANKDT so it fires once) runs on CR Check/Submit:
  `ZCL_MDMDOC_MDG_READER` reads CR attachments (GOS) + CR fields
  (BP_CENTRL/ADDRESS/BP_BANKDT/BP_IBAN/BP_TAXNUM → SAP_KEYS; the mapping is
  NOT hardcoded — `ZCL_MDMDOC_MDG_MAP` defaults overlaid by the optional
  customizing table `ZMDMDOC_MAP`, auto-proposed by `ZMDMDOC_MDG_DISCOVER`).
  The PDF attachment runs through the deterministic pipeline (LLM OFF — the
  BAdI is synchronous, no outbound HTTP) → `ZCL_MDMDOC_COMPARE` (same
  SAP-000..008 logic as Python) → findings appear as WARNINGS in the CR
  message log in Fiori "My Change Requests" (masked values; the NOTE-severity
  SAP-000 all-match finding is skipped). W does not block submit.
  `emit_finding` hardcodes verdict_effect REJECT → message type 'E', but no
  SAP-000..008 compare finding carries REJECT today, so in practice the flow
  emits warnings only; making hard mismatches block would require a code
  change (verify-on-system). Onboarding:
  `ZMDMDOC_SETUP` one-shot (pre-flight self-tests via `ZCL_MDMDOC_SELFTEST`,
  MDG discovery, live CR read, optional mapping save) — activate the BAdI
  only after GO. Everything MDG-specific is marked verify-on-system in the
  ABAP docs (CHECK_ENTITY signature, read_entity_data_all, GOS API, entity
  names).
- **Cross-session rule**: the ABAP repo is co-owned by a parallel session;
  handoffs go via PARITY.md "Coordination requests" — never edit the other
  side's uncommitted files.

## 11. Model client & host resolution (`model_client.py`)

Roles (env `MDMDOC_<ROLE>`, legacy `MDM_VAL_*` fallback): VISION =
`qwen2.5vl:7b`, TEXT = `mdmdoc-extract` (the custom model; stock `qwen3:4b`
is only the fallback), TEXT_STRONG = `qwen3:14b`, EMBED = `nomic-embed-text`
(**vestigial** — `embed()` currently has no callers; few-shot selection is
scenario-coverage-based). Fallback chains per role; `strong_distinct()`
suppresses pointless escalation when STRONG resolves to the same model.

Host resolution (never starts a server anywhere): 1) `MDMDOC_OLLAMA_HOST` /
`OLLAMA_HOST` env; 2) an existing tunnel at `http://127.0.0.1:11435`;
3) auto-open `ssh -f -N -L 11435:127.0.0.1:11434 mac-mini` (BatchMode,
ExitOnForwardFailure); 4) local `:11434` if already running.

`keep_alive=0` everywhere (the mini runs `OLLAMA_MAX_LOADED_MODELS=1`;
sequential single-model use); explicit `unload()` between stages;
`reset_host()` before server jobs so a dead tunnel re-probes. `think=false`
for qwen3 (thinking tokens would eat the answer budget). Vision `num_ctx`
16384 (image tokens overflow the 4096 default — a real SAP-screenshot
HTTP-400 case); `LAST_VISION_ERROR` surfaces the real Ollama error body.

## 12. How to deploy each surface

### 12.1 Mini production (the single running instance)

- LaunchAgent `com.victor.mdmdoc` on the Mac mini, port **8766**, exposed via
  `tailscale serve` (not funnel) → `https://omen.tail461272.ts.net:8766/ui`.
- Env: `MDMDOC_API_TOKEN` (bearer/cookie auth) + `MDMDOC_ALLOWED_HOSTS`
  (allows the tailnet Host header).
- Rules gate is live: `enforce_approvals=true` (`MDMDOC_RULE_GATE` defaults
  on; the off-switch is documented in an `api.py` comment — set
  `MDMDOC_RULE_GATE=0` in the plist and reload). Approvals live in
  `rules/approvals.json`; the panel is `/ui/rules/approve`.
- Deploy flow = the `mdmdoc-deploy` skill. `approvals.json` is excluded from
  the deploy rsync so the mini's live decisions survive updates. On the mini
  do NOT init the `abap/` submodule (the deploy key is scoped to this repo;
  the runtime never reads ABAP sources).
- MacBook-local variant: `scripts/install-launchagent.sh` installs
  `com.egor.mdmdoc` on 127.0.0.1:8766 (see
  [OPERATOR_GUIDE_RU.md](OPERATOR_GUIDE_RU.md)).

### 12.2 Corp compose (FULL mode, sealed host)

See **[CORP_DEPLOY.md](CORP_DEPLOY.md)** for the compose runbook. This is
NOT an api-only deployment: `btp/compose.full.yaml` runs the validator in
**FULL mode** (`MDMDOC_MODE=full` — operator console + teach/train/eval
routes) plus a **co-located Ollama** on one private compose network, on a
sealed corporate host with **zero egress** (`MDMDOC_WEB_EVIDENCE=0`;
`MDMDOC_OLLAMA_HOST` pinned to the ollama container; models provisioned
offline via `scripts/bundle-models.sh`). Auth/roles live in the corporate
reverse proxy/SSO; the app adds `MDMDOC_API_TOKEN` + the FULL-mode
Host-header allowlist. `MDMDOC_BANK_VALUES=masked` is optional if corp
policy forbids showing account/IBAN values.

### 12.3 BTP / Kyma (api-only Docker)

See [BTP_INTEGRATION.md](BTP_INTEGRATION.md) for the full integration guide.
Facts to keep in mind: the image runs api-only (`MDMDOC_MODE=api-only`,
teach/UI routes absent, banking values masked, `MDMDOC_WEB_EVIDENCE=0`
sealed), python:3.12-slim + tesseract, non-root, port 8080, HEALTHCHECK
`/health`, mount only `/app/runs` + `/app/inbox`; committed OpenAPI at
`btp/openapi.json` (`scripts/export-openapi.py`); CF manifest + Kyma yamls
(APIRule noAuth→jwt for prod). Model topologies honestly assessed in
BTP_INTEGRATION.md: (a) on-prem Ollama via Cloud Connector — needs a small
proxy wiring change, not implemented; (b) SAP AI Core — does NOT work today
(API mismatch, no adapter promised); (c) Ollama sidecar in Kyma — works
as-is, needs ~12–16 GiB / GPU realistic. `instances: 1` (run history is
instance-local; `PIPELINE_LOCK` allows one pipeline at a time). No
deployment is performed from this repo — a conscious non-goal.

Both containerized surfaces build from the same `btp/Dockerfile`; the mode
env var is what separates them.

### 12.4 SAP (abapGit import)

See **[SAP_READINESS.md](SAP_READINESS.md)** — the single
done / verify-on-system / not-implemented status list for importing ZMDMDOC
into a target S/4HANA/MDG system, and `abap/docs/INTEGRATION.md` for the
step-by-step (import → activate → ABAP Unit green → `ZMDMDOC_SETUP` GO →
only then MDG BAdI activation).

## 13. Key invariants (memorize these)

1. The model never decides compliance — YAML rules only.
2. A human approves every rule before it can fire; any edit reverts it to
   pending (re-approve).
3. TIN/SSN/EIN are masked everywhere under every policy; banking values are
   full only in the local operator console.
4. Every persisted byte and every outbound query passes a leak gate that
   raises.
5. Web evidence is advisory NOTE-only and structurally cannot move a verdict.
6. Eval measures the raw machine (no precedents, no web, no approvals gate)
   and hard-fails on any leakage.
7. Retrains land as candidates only — Adopt/Rollback is an explicit operator
   action.
8. run id = content hash; correcting a document sticks immediately via
   precedent.
9. Python and ABAP are two targets of one logic — rule data auto-syncs, logic
   hand-ports with receipts, `check_parity.py` fails loudly on drift.
10. No component ever auto-starts an Ollama server.

## 14. The D-wave: operator console + a learning loop that actually learns (2026-07-10)

### 14.1 Run control plane (`runctl.py`)
`run_check(cancel=, on_stage=, overrides=, effort=, template_path=)` binds a
`RunControl` into a **ContextVar** for the duration of the run — thread-isolated
by construction (each server job is its own thread), zero env mutation.
`runctl.checkpoint(stage, pct)` reports progress and raises `CheckCanceled`
cooperatively; checkpoints sit between pipeline stages, between vision calls
and before the strong tier. A canceled run writes **no artifacts**.
Config knobs (`sig_vision_cap`, `ladder_*`, `time_budget_s`) resolve
`run override > env > default`.

### 14.2 Queue, cancel, progress
`jobs.PipelineGate` (over the same `PIPELINE_LOCK`) gives check jobs FAIR FIFO
admission with visible positions; `POST /api/v1/jobs/{id}/cancel` kills a
queued job instantly and a running one at its next checkpoint (an in-flight
Ollama call finishes first). The dashboard shows a live queue panel (position,
stage, per-job progress, Cancel) and a `<progress>` bar driven by checkpoint
percents + the estimate ETA. Multi-file drops enqueue one job per file.

### 14.3 Effort slider (replaces thorough/engine controls)
`config.effort_profile(1..5)`: 1 deterministic instant → 3 today's auto
(byte-identical) → 5 llm-first + extended strong-tier thinking
(`num_predict` 4096, `think=true`) + all signature probes + a deeper ladder.
The FAST tier's model call is **byte-frozen** (regression-tested) — effort may
widen only the strong tier. `settings.json: default_effort`; `meta.effort`.

### 14.4 Reasoning export
Every run writes `reasoning.md` — the full decision trace (perception, tiers,
every guard warning, signature votes/trail, ladder, confidence reasons, a
table of EVERY rule evaluated incl. approval-gate skips via
`run_rules(trace=[])`, compare tables, the verdict fold). Always MASKED and
strict-scrubbed: its purpose is to be pasted into an external LLM for critique.

### 14.5 Containers and the template stream
`.msg` parses for real now (olefile MAPI reader); `.xlsx/.xlsm` with
`xl/embeddings` become CONTAINERS (the request workbook's embedded support
docs are the evidence); a request-form workbook with no embedded docs raises a
clear error pointing at the Template slot. `template_form.py` parses the
filled request workbook (generic label→value scan, no pinned coordinates) and
`sap_compare.compare(prefix="TPL")` renders a second compare stream
(`TPL-00x` findings, fail-closed `TPL-014`, `template_compare.json`).

### 14.6 Unified rules file + skill sources
`rules/rules.yaml` — the ONE physical rules file: multi-document YAML with
`--- # doc_class: X` sections whose text stays byte-identical to the old
per-class files (approval hashes and comments survive; the textual splice
tooling is unchanged). `rules_io.rules_text/save_rules/save_unified` are the
only readers/writers; a legacy two-file checkout still works as a fallback.
`skill_import.py` attaches ANY skill as a rule source: checker skills parse
deterministically, arbitrary text goes through the strong model; every
imported rule lands PENDING (`source: skill:<name>`), re-import replaces only
that skill's own marked segment.

### 14.7 The closed learning loop
* fresh few-shot exemplars inject at runtime for EVERY model including the
  baked `mdmdoc-extract` (deduped against `models/exemplar_values.json`) — a
  correction reaches the NEXT document immediately;
* a label NOTE is auto-classified (`learning.note_to_rule`): a clean ADDITIVE
  rule proposal is appended as PENDING (source operator, tier learned);
  anything touching existing rules queues in `rules/proposals.jsonl`;
* "Mark valid ✓" = a confirmed ACCEPT label in one click;
* `dataset/patterns.jsonl` records a PII-free shape fingerprint per label;
  ≥3 matching valid-marks add a `PATTERN-1` NOTE and damp ONE weak confidence
  signal (medium→high only — hard signals and verdicts are untouched);
* 👍/👎 (`dataset/ratings.jsonl`): a 👎 tops the training queue and flags the run;
* `error_source` finally routes: `err_<source>` scenario tags + `rule_wrong`
  feeds the note-to-rule channel.

Invariant unchanged everywhere: **verdicts come only from human-approved
rules** — every learning channel produces PENDING artifacts or informational
signals, never a verdict change.
