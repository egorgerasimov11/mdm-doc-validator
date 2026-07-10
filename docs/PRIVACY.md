# Privacy & Data Handling

The documents this tool reads carry exactly the data that must not leak: bank
account numbers, IBANs, routing numbers, SSN/EIN tax identifiers. The design
treats privacy as an *invariant enforced by code*, not a policy hope.

## The two-policy model (updated)

Two value families, each with its own *display policy*. Both default to `full`
in the operator console and `masked` in the api-only/BTP image:

- **Banking identifiers (account number, routing/ABA, IBAN)** — `MDMDOC_BANK_VALUES`.
- **Tax numbers (TIN/SSN/EIN, W-8 foreign/US TIN)** — `MDMDOC_TIN_VALUES`.
  Revealed on the console because the operator types them into SAP and the
  source document — which shows the number in plain sight — is one click away
  behind *Download document*. Masking the field while shipping the PDF that
  contains it bought no protection and cost a transcription step.

The leak gate for run artifacts blocks exactly the families the display still
masks (`config.gate_policy()`: `strict` → `tin-only` → `none`), so a value shown
in full can never trip the gate on its own legitimate content.

Revealing a family on the console reaches **nothing else**. Four separate
mechanisms keep it contained, each locked by a test in `tests/test_tin_reveal.py`:

1. **`reasoning.md`** — the decision trace built to be pasted into an external
   LLM. It is assembled from `to_public(policy="masked")` and then run through
   `scrub_text(policy="strict")`. A caller that asks for `masked` by name is
   never overridden by configuration, so this artifact carries no tax number no
   matter how the console is configured.
2. **Training data is ALWAYS strict** — `dataset/labels.jsonl`,
   `prompts/fewshot/`, `dataset/mlx-lora/` never carry full values: the label
   builder strips the `value` key on *keep* and re-masks anything typed on
   *set*, and their gates fail closed.
3. **Outbound web verification** — `web_enrichment/egress.py` forbids `TIN_KINDS`
   unconditionally; a tax number is never a query parameter.
4. **BTP / api-only** — the SAP-facing deployment masks both families.

## Invariants

1. **Sensitive values persist into local run artifacts only under the
   operator's explicit `full` display policy.** They never enter training data,
   never leave over the network, and never appear in the BTP image defaults.
2. **Every persisted byte passes a leak gate.** `runstore.write()` (all run
   artifacts) and `dataset.append_label()` (training data) call
   `privacy.assert_no_leak()`, which scans for known full values *and* generic
   patterns (SSN/EIN shapes, full IBANs, long digit runs) and **raises** on a
   hit — a leaking write crashes instead of leaking. This is not theoretical:
   during development the gate blocked an unmasked routing number in the SAP
   comparison table; the fix was masking at the source, never relaxing the gate.
3. **Eval enforces zero leakage.** `mdmdoc eval` sweeps `runs/`, `dataset/`,
   `prompts/`, `eval/` and hard-fails (non-zero exit) if `leakage_count > 0`.
   Read the number honestly: `dataset/`, `prompts/` and `eval/` are always swept
   `strict`, and those are the trees that get committed. `runs/` is swept under
   the display policy, so on a console with both families revealed the sweep of
   `runs/` proves nothing — by design, because those artifacts are *allowed* to
   carry the values. `runs/` is gitignored and local.

## The masking model (`src/mdmdoc/privacy.py`)

| kind | full (memory only) | persisted/displayed |
|---|---|---|
| SSN | 900-XX-0693 (illustrative) | `XXX-XX-0693` (hyphen style preserved — an SAP entry rule) |
| EIN | XX-XXX6789 (illustrative) | `XX-XXX6789` |
| IBAN | DE44…4931 (22 ch) | `DE**…4931` + derived facts `{country, length, shape_ok}` |
| account / routing | 1830042757 | `…2757` + `{length}` |

Free text (OCR excerpts, error messages, report evidence) goes through
`scrub_text()`, which also catches spaced/hyphenated/one-digit-per-line
variants (the W-9 digit-box case) of every value seen in the run.

## Fakes vs masks (training data)

Few-shot exemplars and LoRA exports must show the model *realistic* values —
masked exemplars would teach it to output masks. So training data uses
**shape-preserving fakes** (`fake_preserve_shape`): deterministic replacements
with the same prefix/length/hyphenation but different digits. Fakes are listed
in each label's `sensitive_map` (masked ↔ fake pairs, no real values) and are
explicitly allow-listed at the leak gate — a real value can never ride through
that allowance because the known-secret pass runs first.

## What is stored where

| location | content | sensitivity | lifecycle |
|---|---|---|---|
| `inbox/` | original uploads (content-addressed) | **raw documents** | gitignored; delete `inbox/<sha16>__*` to erase |
| `runs/<sha16>/` | meta, OCR excerpt (scrubbed), extraction, findings, reports, sap_compare, web_evidence | masked only | gitignored; delete the folder to erase |
| page renders | pixels of pages | raw | deleted after every run; UI preview **and evidence crops** (W-9 checkbox/TIN-box, signature, bank-line zones) render on demand into a temp dir with `Cache-Control: no-store`, never persisted; both endpoints live on the teach router only (absent in BTP) |
| `extraction.json` provenance | per-field `{source, page}` tags (model / ocr-regex / zone-probe / vision-crop / rule / precedent) | no values | part of the run artifacts above |
| `dataset/labels.jsonl` | training examples | masked + fakes | committable by design |
| `prompts/fewshot/` | exemplars | fakes only | committable |
| server logs | method, path, run ids, file names | no values | in-memory ring (500 lines) |

The Docker image ships **none** of the operator's data (`.dockerignore`
excludes runs, inbox, labels, eval history).

## Outbound network egress (external evidence)

The optional external-evidence layer (`web_enrichment`, opt-in via
`MDMDOC_WEB_EVIDENCE=1`, **off in the BTP image**) is the only part of the tool
that talks to the network. It has its own outbound choke point,
`web_enrichment.egress.assert_safe_outbound`, the mirror image of the inbound
leak gate:

- **Only these identifiers may leave the machine:** routing/ABA numbers,
  SWIFT/BIC codes, bank names, company names.
- **Never sent:** full TIN/SSN/EIN, bank account numbers, IBANs. The guard
  reuses `privacy.assert_no_leak` with a forbidden set built from the run's
  vault (account + IBAN + every TIN kind) plus the strict generic patterns, so
  a would-be leak **raises** before any socket opens. A 9-digit routing number
  matches none of the forbidden patterns and passes; an account/IBAN/TIN does
  not.
- **Every request goes through `web_enrichment.http`** (trust_env=False, short
  timeout, descriptive User-Agent), which calls the egress guard on the rendered
  URL before sending and returns `None` on any failure (offline degrades to an
  `unavailable` hint — it never crashes a run or changes a verdict).
- **Advisory only:** every external finding is severity `NOTE` with no verdict
  effect; the run page shows a permanent banner, "web did not decide this
  verdict." See `docs/WEB_EVIDENCE.md`.

## Sensitive values in transit

The review/label API accepts full values when the operator corrects a field
(`action: "set"`). That request body exists in memory only, on
127.0.0.1 in operator mode; the api-only BTP image does not register the
endpoint at all. No middleware logs request bodies; the operator UI never
echoes a typed full value back.

## Erasure & audit

- **Erase one document:** delete `inbox/<sha16>__*`, `runs/<sha16>/`, and (if
  labeled) its line in `dataset/labels.jsonl` (`doc_sha256` field). Nothing
  else references it.
- **Audit what was decided and why:** `runs/<sha16>/findings.json` holds the
  fired rule ids and messages; `report.json` fixes verdict, model id and
  timestamp — all masked, safe to attach to a case.
- **Model training data audit:** `dataset/labels.jsonl` is human-readable;
  every sensitive entry carries `present`/`masked`/derived facts only.

## Known residual risks

- OCR scrubbing of *unknown* values relies on generic patterns; exotic digit
  formatting could evade them. Defense in depth: excerpts are truncated, runs/
  is local and gitignored, and the eval sweep re-checks everything.
- `--keep-renders` (debug flag) keeps page pixels on disk — documented as
  sensitive, off by default.
- The SAP screenshot contains full bank data by nature; it is treated exactly
  like a document upload (inbox, masked artifacts, in-memory values).
