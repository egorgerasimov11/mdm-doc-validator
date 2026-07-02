# API Reference

Base URL: `http://<host>:<port>/api/v1` · OpenAPI: `GET /openapi.json`
(the committed api-only contract lives at [`btp/openapi.json`](../btp/openapi.json)).

## Authentication

Bearer token, enabled by setting `MDMDOC_API_TOKEN` on the server:

```
Authorization: Bearer <token>
```

Optional on localhost (operator console), **mandatory for any BTP/remote
deployment**. For production, front the service with XSUAA/AppRouter or a Kyma
APIRule jwt handler and keep the token as defense-in-depth
(see [BTP_INTEGRATION.md](BTP_INTEGRATION.md)).

## Error model

Non-2xx responses carry:

```json
{"error": {"code": "unreadable_document", "message": "password-protected PDF — ..."}}
```

| code | HTTP | meaning |
|---|---|---|
| `bad_request` | 400 | invalid parameters / leak-gate rejection |
| `unauthorized` | 401 | missing/invalid bearer token |
| `forbidden_host` | 403 | non-local Host header in operator mode |
| `not_found` | 404 | unknown run / artifact / job |
| `unreadable_document` | 422 | locked PDF, unrenderable file |
| `job_conflict` | 409 | an eval/check job is already running |
| `model_host_down` | 503 | no Ollama-compatible endpoint reachable |
| `internal` | 500 | unexpected error (message is privacy-scrubbed) |

Every error message passes through the privacy scrubber before serialization.

## Core endpoints (present in the BTP image)

### `GET /health` *(unversioned, unauthenticated)*
Liveness only — never probes the model host. Docker HEALTHCHECK target.

### `GET /api/v1/doctor`
Model host (url/source/reachable), role → resolved model table, tesseract langs,
directory status, labels/runs counts. Degrades to `reachable: false` instead of
erroring; a failed probe resets the cached host so the next call re-tunnels.

### `POST /api/v1/check` — validate a document
`multipart/form-data`:

| field | type | notes |
|---|---|---|
| `file` | file | PDF or image (required unless `rerun_run_id`) |
| `doc_class` | `bank` \| `w9` | required |
| `lang` | `en` \| `ru` | rule messages / report language (default en) |
| `use_vision` | bool | default true; false = tesseract-only perception |
| `wait` | bool | **true** (default): synchronous — the response is the result; set client timeout ≥ 300 s. **false**: returns `202 {"job_id"}` — poll jobs |
| `sap_file` | file | optional (bank only): SAP Bank Details screenshot → comparison |
| `rerun_run_id` | str | re-run a stored document (used by "Compare with SAP" after the fact) |

Successful synchronous response:

```json
{"run_id": "a0c5cbc35b7ac55b", "verdict": "ACCEPT",
 "report": { ...mdmdoc.v1... }, "report_md": "[BANK DOC VERDICT]\n..."}
```

Uploads are stored content-addressed as `inbox/<sha16>__<name>`; re-uploading
the same bytes reuses the file and the run id (= sha256[:16] of the content).

### `GET /api/v1/runs`, `GET /api/v1/runs/{run_id}`, `GET /api/v1/runs/{run_id}/artifacts/{name}`
Run history, full detail (meta/extraction/findings/report), and raw artifacts
(strict allowlist: `meta.json stage_a.json extraction.json findings.json
report.json report.md sap_compare.json` — no path traversal).

### `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}?after=N`
Background job polling; `after` returns only new progress lines.

### `GET /api/v1/rules?doc_class=bank|w9`
The active YAML rule set, parsed.

## Teach endpoints (operator only — absent in api-only mode)

| route | purpose |
|---|---|
| `GET /api/v1/runs/{id}/preview/{page}?src=doc\|sap` | on-demand page render (never persisted) |
| `GET /api/v1/runs/{id}/review` | review-form defaults (field keys, display values, taxonomies) |
| `POST /api/v1/runs/{id}/label` | submit corrections → masked training example |
| `GET /api/v1/labels` | the (masked) dataset |
| `POST /api/v1/train/fewshot` `{k}` | rebuild few-shot exemplars |
| `POST /api/v1/train/modelfile` `{apply}` | build/apply the custom Ollama model |
| `POST /api/v1/train/export-lora` | MLX LoRA dataset export (gated at 100 labels) |
| `POST /api/v1/eval` `{tag, only, limit}` | start an eval job (409 if one runs) |
| `GET /api/v1/eval/history`, `GET /api/v1/eval/report` | metrics history / latest report |

`POST .../label` body (`ReviewSubmission`): per-field
`{"action": "keep"|"set"|"clear", "value": ...}` — a `set` on a sensitive field
may carry the full value; it exists in request/process memory only and is
persisted masked (see [PRIVACY.md](PRIVACY.md)).

## The mdmdoc.v1 report schema

```json
{
  "schema": "mdmdoc.v1",
  "run_id": "sha256[:16] of the document",
  "doc_class": "bank | w9",
  "doc_type": "bank_letter | supplier_letterhead | bank_screenshot | voided_check |
               ap_document | invoice | email | editable_source | other |
               w9 | w8 | other_tax | unknown",
  "verdict": "ACCEPT | REJECT | WARNING | NEED_MANUAL_REVIEW",
  "next_step": "human-readable next action",
  "findings": [{"rule_id": "BNK-001", "severity": "CRITICAL|WARNING|NOTE",
                "verdict_effect": "REJECT|NEED_MANUAL_REVIEW|WARNING|null",
                "message": "...", "detail": "..."}],
  "fields": { "...masked extraction, see below..." },
  "sap_compare": [{"field": "IBAN", "doc": "DE**…4931", "sap": "DE**…4931",
                   "status": "match|MISMATCH|only-one-side|sap-only|check",
                   "note": "differs from position 12"}],
  "crosscheck": ["account_number=confirmed", "iban=filled-from-OCR(DE**…4931)"],
  "sensitive_present": {"iban": true, "tin": true},
  "model": "qwen3:4b", "json_valid_first_try": true, "ts": "ISO-8601"
}
```

Masked sensitive field shapes inside `fields`:

| field | shape |
|---|---|
| `iban` | `{"masked": "DE**…4931", "country": "DE", "length": 22, "present": true}` |
| `account_number`, `routing_aba` | `{"masked": "…2757", "length": 10, "present": true}` |
| `tin` (W-9) | `{"type": "SSN\|EIN", "masked": "XXX-XX-0693", "digits": 9, "hyphenated": true, "present": true}` |

Names, bank names, addresses and dates are plain strings (not PII-masked).

## Verdict semantics

The HTTP status of a successful check is always 200 — **the verdict is payload,
not transport**. CLI exit codes map: `0` ACCEPT, `1` REJECT, `2`
WARNING/NEED_MANUAL_REVIEW, `3` model host down, `4` unreadable input.

## End-to-end example

```bash
TOKEN=...; HOST=https://mdmdoc.example.com
# validate a bank document together with an SAP Bank Details screenshot
curl -s -H "Authorization: Bearer $TOKEN" \
     -F "file=@bank_letter.pdf" -F "doc_class=bank" -F "sap_file=@sap_screen.png" \
     "$HOST/api/v1/check" | jq '.verdict, .report.sap_compare'
# fetch the human report later
curl -s -H "Authorization: Bearer $TOKEN" \
     "$HOST/api/v1/runs/<run_id>/artifacts/report.md"
```
