# Architecture

mdmdoc is a local-first document validator for vendor-master (MDM) work: it
classifies **banking support documents** and **US W-9/W-8 tax forms**, extracts
their fields with local LLMs, applies an **explicit, human-approved rule set**,
and returns a verdict with the extracted data. This page is the compact map;
implementation depth lives in [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md). One
codebase serves three faces:

| Face | Entry | Consumer |
|---|---|---|
| CLI | `mdmdoc check / check-bank / check-w9 / review / train / eval / export-lora / runs / doctor / skill-rules / ui / serve` | operator, scripts |
| Operator console | `mdmdoc ui` → `http://127.0.0.1:8766/ui` (prod: mini, `tailscale serve`) | the MDM analyst |
| REST API | `mdmdoc serve --api-only` (Docker: `btp/Dockerfile`) | corp/BTP integration |

A fourth surface is a separate repo: the **ABAP twin** `ZMDMDOC`
(`abap/` submodule), the deterministic in-MDG validator — see below.

```
                 ┌──────────────────────────── engine ────────────────────────────┐
 document ──►  Stage A (frozen perception)          Stage B (trainable extraction)
               • page survey: score ALL pages,      • FAST tier: custom model
                 deep-read only the best ones         `mdmdoc-extract` (qwen3:4b
               • text layer / tesseract 300dpi /      fallback) → {doc_type, fields}
                 vision transcription (qwen2.5vl)   • STRONG tier (qwen3:14b) on
               • OSD rotation fix for photos          escalation; merge never blanks
               • regex IDs: IBAN/SWIFT/EIN/SSN…     • ~8 deterministic guards +
               • signature probe + W-9 zone probes    OCR cross-check outrank the
                 (checkbox row, TIN boxes)            model on IDs; probes settle
                        │                             signature/checkbox/TIN
                        └──────────────┬─────────────────────┘
                                       ▼
                        rules engine (rules/*.yaml, YAML-editable)
                                       ▼
                        APPROVALS GATE (rule_approvals.py) — a rule fires only if
                        human-approved; pending applicable rule ⇒ RULE-GATE finding
                        holds the document at NEED_MANUAL_REVIEW
                                       ▼
                        verdict fold: REJECT > NEED_MANUAL_REVIEW > WARNING > ACCEPT
                        (+ operator precedent for this content hash overrides)
                                       ▼
                        report.md + report.json (schema mdmdoc.v1)
                                       ▼
                        web evidence (opt-in, AFTER the verdict, NOTE-only —
                        structurally cannot change the verdict)
               └──────────────────────────────────────────────────────────────────┘
 optional: SAP comparison — vision reads a Bank Details screenshot (future: live
 MDG data) → deterministic char-by-char compare → SAP-000..008 findings
```

## Design principles

1. **The model never decides compliance.** It classifies and extracts;
   verdicts come only from `rules/banking.yaml` / `rules/w9.yaml` via
   `src/mdmdoc/rules/engine.py`. Rules are data, reviewable and auditable.
2. **A human approves every rule.** `rule_approvals.py` +
   `rules/approvals.json` + the `/ui/rules/approve` panel: a rule can only
   fire after explicit approval; editing a rule reverts it to pending; an
   un-approved applicable rule holds the document at NEED_MANUAL_REVIEW
   (`MDMDOC_RULE_GATE`, default on).
3. **Critical identifiers are deterministic.** IBAN/SWIFT/EIN/SSN/routing come
   from literal OCR regex (`ocr.regex_fields`, `fields.find_boxed_tin`); the
   model's reading is cross-checked and overridden on mismatch
   (`fields.crosscheck_ids`), and a pack of ~8 named deterministic guards in
   `stage_b.py` (parity-tracked, hand-ported to ABAP) repairs model errors.
   Vision probes (signature; W-9 checkbox/TIN boxes) settle their fields over
   any text guess.
4. **Privacy by construction.** Full sensitive values exist only in process
   memory; every persisted byte flows through the masking choke point and the
   leak gate; every outbound query passes an egress gate (see
   [PRIVACY.md](PRIVACY.md)).
5. **Search until found.** Multi-page documents are surveyed page-by-page and
   scored; sideways photos are rotation-corrected; a bank document with no IDs
   after the first pass triggers a targeted second vision pass.
6. **Trainable where it pays.** Only Stage B learns — corrections become
   few-shot exemplars baked into the custom `mdmdoc-extract` model, retrains
   land as **candidates** behind an adoption gate (gated eval →
   Adopt/Rollback), and LoRA is gated at 100 labels (see
   [../TRAINING.md](../TRAINING.md)).
7. **Web evidence never decides.** The opt-in `web_enrichment/` layer emits
   NOTE-only findings from public registries; verdict_effect is hard-coded
   to none ([WEB_EVIDENCE.md](WEB_EVIDENCE.md)).
8. **One logic, two targets.** The ABAP twin shares rule DATA (generated from
   the same YAML) and hand-ported logic with receipts; `tools/check_parity.py`
   fails loudly on drift ([SYNC.md](SYNC.md), [../PARITY.md](../PARITY.md)).

## Doc-type taxonomy

Bank: `bank_letter, bank_statement, supplier_letterhead, bank_screenshot,
voided_check, payment_instructions, ap_document, invoice, email,
editable_source, other`. W-9 class: `w9, w8, other_tax, unknown`. Only three
auto-REJECTs exist: invoice, email, editable file (BNK-001/002/003); W-9
issues never hard-reject. (Known discrepancy: `banking.yaml`'s `doc_types:`
list omits `bank_statement`/`payment_instructions` — see DEVELOPER_GUIDE §4.1.)

## Components

| Path | Role |
|---|---|
| `src/mdmdoc/pipeline.py` | `run_check()` end-to-end orchestrator; container unwrap; precedent |
| `src/mdmdoc/stage_a.py` | perception: survey, rotation, OCR, vision transcription, probes |
| `src/mdmdoc/stage_b.py` | trainable extraction (FAST/STRONG tiers) + deterministic guard pack |
| `src/mdmdoc/ocr.py` | tesseract + deterministic ID regex (vendored, battle-tested) |
| `src/mdmdoc/fields.py` | field contracts, page scoring, cross-check, IBAN utilities |
| `src/mdmdoc/rules/` | YAML rule engine + predicate registry |
| `src/mdmdoc/rule_approvals.py` | the human-approval hard gate (`rules/approvals.json`) |
| `src/mdmdoc/rule_propose.py` | propose-only rule changes (dispute → validated YAML diff) |
| `src/mdmdoc/rules_io.py` | the ONLY rule writer; `regenerate_abap()` |
| `src/mdmdoc/verdict.py` | precedence fold REJECT > NEED_MANUAL_REVIEW > WARNING > ACCEPT |
| `src/mdmdoc/report.py` | EXTRACTED DATA / SAP COMPARISON blocks, mdmdoc.v1 JSON |
| `src/mdmdoc/sap_compare.py` | doc ↔ SAP Bank Details comparison (source-agnostic) |
| `src/mdmdoc/web_enrichment/` | NOTE-only external evidence + egress gate |
| `src/mdmdoc/privacy.py` | mask / scrub / fakes / leak gate — the choke point |
| `src/mdmdoc/runstore.py` | `runs/<sha16>/` artifacts; every write leak-gated |
| `src/mdmdoc/review_core.py` | programmatic review (CLI and web are thin wrappers) |
| `src/mdmdoc/scenarios.py` | failure-shaped scenario tags + auto-suggestion |
| `src/mdmdoc/training_queue.py` | ranks runs worth labeling next |
| `src/mdmdoc/{fewshot,modelfile,adoption,evalrun,lora_export}.py` | teach loop: coverage few-shot, custom model, candidate/adoption gate, eval, LoRA export |
| `src/mdmdoc/dataset.py` | labels.jsonl + erasure (+ portable corpus, in flight) |
| `src/mdmdoc/evidence.py` | UI evidence crops (deterministic page+zone, never persisted) |
| `src/mdmdoc/estimate.py` | duration estimates per run shape |
| `src/mdmdoc/skill_rules.py` | read-only checker-skill parser (`mdmdoc skill-rules`) |
| `src/mdmdoc/model_client.py` | roles, host resolution, unload |
| `src/mdmdoc/cli.py` | all CLI entry points, verdict → exit-code mapping |
| `src/mdmdoc/compare.py` | v2 STUB: vendor-template (.xlsx) comparer protocol — not implemented in v1 by design |
| `src/mdmdoc/server/` | FastAPI app factory, API routers, jobs, operator UI |
| `btp/` | Dockerfile, CF manifest, Kyma yamls, exported openapi.json |
| `tools/check_parity.py` | Python↔ABAP drift detector (see [SYNC.md](SYNC.md)) |
| `abap/` | submodule pin of the ABAP twin `ZMDMDOC` |

## Server architecture

`create_app(mode)` (`src/mdmdoc/server/app.py`):

- **full** (default; `mdmdoc ui`): operator UI (server-rendered Jinja2 +
  vanilla JS, no build step) + complete API. Binds 127.0.0.1; a Host-header
  check rejects DNS-rebinding, with `MDMDOC_ALLOWED_HOSTS` exceptions — this
  is how production is exposed on the tailnet via `tailscale serve`
  (mini: LaunchAgent `com.victor.mdmdoc`, port 8766,
  `https://omen.tail461272.ts.net:8766/ui`).
- **api-only** (`mdmdoc serve --api-only`, the Docker image): core API only —
  UI, review/label, training, eval, page-preview and evidence-crop routes are
  **not registered**, so the served OpenAPI is honest about the deployed
  surface.

Auth: bearer token when `MDMDOC_API_TOKEN` is set (constant-time compare); the
UI drops an httponly cookie so page sub-resources authenticate; `/health` is
unauthenticated liveness. Long operations run as in-process jobs
(`server/jobs.py`, daemon threads, polling via `/api/v1/jobs`).
`PIPELINE_LOCK` serializes all model work: the host loads one model at a time
(`OLLAMA_MAX_LOADED_MODELS=1`). Endpoint list: [API.md](API.md) +
DEVELOPER_GUIDE §8.

## Model host resolution

`model_client.host()` (never starts a server anywhere):

1. `MDMDOC_OLLAMA_HOST` / `OLLAMA_HOST` env — explicit override, no fallback;
2. an already-open SSH tunnel at `http://127.0.0.1:11435`;
3. auto-open the tunnel `ssh -f -N -L 11435:127.0.0.1:11434 mac-mini`;
4. a locally running Ollama at `:11434`.

Model roles (env-overridable): `MDMDOC_VISION` (qwen2.5vl:7b), `MDMDOC_TEXT`
(**`mdmdoc-extract`** — the custom trained model; stock qwen3:4b is only the
fallback), `MDMDOC_TEXT_STRONG` (qwen3:14b — the escalation tier),
`MDMDOC_EMBED` (nomic-embed-text — currently vestigial, no callers; few-shot
selection is scenario-coverage-based). All calls use `keep_alive=0` with
explicit `unload()` between stages. A long-lived server calls `reset_host()`
before jobs so a dead tunnel re-probes instead of failing forever.

## Data lifecycle

```
upload → inbox/<sha16>__<name>       (raw document, gitignored, content-addressed)
run    → runs/<sha16>/               (masked artifacts: meta, stage_a, extraction,
                                      findings, report.md/json, sap_compare,
                                      web_evidence)
label  → dataset/labels.jsonl        (masked + shape-preserving fakes, committable)
       → dataset/corpus/             (portable labeled corpus — in flight,
                                      MDMDOC_CORPUS_DIR; re-check before relying)
train  → prompts/fewshot/*.json      (fake values only)
       → models/Modelfile.*          (custom-model recipes; previous kept for rollback)
       → models/adoption.json        (candidate/adopted state, leak-gated)
eval   → eval/last_results.json + history.jsonl + report.md
       → eval/candidate_results.json + candidate_report.md   (gate evals — never
                                      recorded into history)
rules  → rules/*.yaml (source of truth) + rules/approvals.json (approval state,
                                      never deployed over, never copied to ABAP)
```

Renders (pixels contain full data) are deleted after each run; document
preview pages and evidence crops in the UI render on demand into temp dirs,
stream with `Cache-Control: no-store`, and are never persisted.

## The ABAP twin, in one paragraph

`abap/` pins the exact commit of `mdm-doc-validator-abap` (`ZMDMDOC`) this
repo was verified against. Rule DATA auto-syncs (panel "Regenerate for SAP" →
the ABAP repo's `tools/gen_rules_abap.py` → generated `ZCL_MDMDOC_RULES_DATA`);
predicate bodies and the Stage-B guard pack are hand-ported with receipts
(`[GUARD:<name>]` markers, PARITY.md manifest); `tools/check_parity.py` fails
on any drift, including a stale submodule pin. The teach loop, web panel and
web evidence are consciously NOT ported. Depth: DEVELOPER_GUIDE §10,
[SYNC.md](SYNC.md), [SAP_READINESS.md](SAP_READINESS.md), and the ABAP repo's
own `docs/`.
