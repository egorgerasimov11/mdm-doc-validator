# Architecture

mdmdoc is a local-first document validator for vendor-master (MDM) work: it
classifies **banking support documents** and **US W-9/W-8 tax forms**, extracts
their fields with local LLMs, applies an **explicit, editable rule set**, and
returns a verdict with the extracted data. One codebase serves three faces:

| Face | Entry | Consumer |
|---|---|---|
| CLI | `mdmdoc check-bank / check-w9 / review / train / eval` | operator, scripts |
| Operator console | `mdmdoc ui` → `http://127.0.0.1:8766/ui` | the MDM analyst |
| REST API | `mdmdoc serve --api-only` (Docker: `btp/Dockerfile`) | SAP BTP integration |

```
                 ┌──────────────────────────── engine ────────────────────────────┐
 document ──►  Stage A (frozen perception)          Stage B (trainable extraction)
               • page survey: score ALL pages,      • small text model (qwen3:4b)
                 deep-read only the best ones         text → {doc_type, fields}
               • text layer / tesseract 300dpi /    • few-shot exemplars injected
                 vision transcription (qwen2.5vl)     from operator corrections
               • OSD rotation fix for photos        • deterministic OCR cross-check
               • regex IDs: IBAN/SWIFT/EIN/SSN…       overrides the model on IDs
                        │                                    │
                        └──────────────┬─────────────────────┘
                                       ▼
                        rules engine (rules/*.yaml, YAML-editable)
                                       ▼
                        verdict: ACCEPT / REJECT / WARNING / NEED_MANUAL_REVIEW
                                       ▼
                        report.md + report.json (schema mdmdoc.v1)
               └──────────────────────────────────────────────────────────────────┘
 optional: SAP comparison — vision reads a Bank Details screenshot (future: live
 BTP data) → deterministic char-by-char compare → extra findings in the verdict
```

## Design principles

1. **The model never decides compliance.** It classifies and extracts; verdicts
   come only from `rules/banking.yaml` / `rules/w9.yaml` via
   `src/mdmdoc/rules/engine.py`. Rules are data, reviewable and auditable.
2. **Critical identifiers are deterministic.** IBAN/SWIFT/EIN/SSN/routing come
   from literal OCR regex (`ocr.regex_fields`, `fields.find_boxed_tin`); the
   model's reading is cross-checked and overridden on mismatch
   (`fields.crosscheck_ids`).
3. **Privacy by construction.** Full sensitive values exist only in process
   memory; every persisted byte flows through the masking choke point and the
   leak gate (see [PRIVACY.md](PRIVACY.md)).
4. **Search until found.** Multi-page documents are surveyed page-by-page and
   scored; sideways photos are rotation-corrected; a bank document with no IDs
   after the first pass triggers a targeted second vision pass.
5. **Trainable where it pays.** Only Stage B learns — from operator corrections
   (few-shot) and eventually LoRA (see [TRAINING.md](../TRAINING.md)).

## Components

| Path | Role |
|---|---|
| `src/mdmdoc/stage_a.py` | perception: survey, rotation, OCR, vision transcription |
| `src/mdmdoc/stage_b.py` | trainable extraction (text → JSON) |
| `src/mdmdoc/ocr.py` | tesseract + deterministic ID regex (vendored, battle-tested) |
| `src/mdmdoc/fields.py` | field contracts, page scoring, cross-check, normalizers |
| `src/mdmdoc/rules/` | YAML rule engine + predicate registry |
| `src/mdmdoc/verdict.py` | precedence fold REJECT > NEED_MANUAL_REVIEW > WARNING > ACCEPT |
| `src/mdmdoc/report.py` | EXTRACTED DATA / SAP COMPARISON blocks, mdmdoc.v1 JSON |
| `src/mdmdoc/sap_compare.py` | doc ↔ SAP Bank Details comparison (source-agnostic) |
| `src/mdmdoc/privacy.py` | mask / scrub / fakes / leak gate — the choke point |
| `src/mdmdoc/runstore.py` | `runs/<sha16>/` artifacts; every write leak-gated |
| `src/mdmdoc/review_core.py` | programmatic review (CLI and web are thin wrappers) |
| `src/mdmdoc/{fewshot,evalrun,lora_export,modelfile}.py` | the teach loop |
| `src/mdmdoc/server/` | FastAPI app factory, API routers, jobs, operator UI |
| `btp/` | Dockerfile, CF manifest, Kyma yamls, exported openapi.json |

## Server architecture

`create_app(mode)` (`src/mdmdoc/server/app.py`):

- **full** (default; `mdmdoc ui`): operator UI (server-rendered Jinja2 +
  vanilla JS, no build step) + complete API. Bound to 127.0.0.1. A Host-header
  check rejects DNS-rebinding attempts.
- **api-only** (`mdmdoc serve --api-only`, the Docker image): core API only —
  UI, review/label, training and eval routes are **not registered**, so the
  served OpenAPI is honest about the deployed surface.

Long operations run as in-process jobs (`server/jobs.py`, daemon threads,
polling via `/api/v1/jobs/{id}`). `PIPELINE_LOCK` serializes all pipeline runs:
the model host loads one model at a time (`OLLAMA_MAX_LOADED_MODELS=1`).

## Model host resolution

`model_client.host()` (never starts a server anywhere):

1. `MDMDOC_OLLAMA_HOST` / `OLLAMA_HOST` env — explicit override, no fallback;
2. an already-open SSH tunnel at `http://127.0.0.1:11435`;
3. auto-open the tunnel `ssh -f -N -L 11435:127.0.0.1:11434 mac-mini`;
4. a locally running Ollama at `:11434`.

Model roles (env-overridable): `MDMDOC_VISION` (qwen2.5vl:7b), `MDMDOC_TEXT`
(qwen3:4b — the trainable one), `MDMDOC_EMBED` (nomic-embed-text). All calls
use `keep_alive=0`. A long-lived server calls `reset_host()` before jobs so a
dead tunnel re-probes instead of failing forever.

## Data lifecycle

```
upload → inbox/<sha16>__<name>       (raw document, gitignored, content-addressed)
run    → runs/<sha16>/               (masked artifacts: meta, stage_a, extraction,
                                      findings, report.md/json, sap_compare)
label  → dataset/labels.jsonl        (masked + shape-preserving fakes, committable)
train  → prompts/fewshot/*.json      (fake values only)
eval   → eval/report.md + history.jsonl
```
Renders (pixels contain full data) are deleted after each run; document preview
in the UI renders pages on demand and never persists them.
