# mdm-doc-validator

Local, privacy-safe CLI that validates **banking support documents** and **US W-9/W-8
tax forms** for SAP MDM work. Everything runs on-device (Ollama + tesseract); documents
never leave the machine, and full tax IDs / account numbers never leave process memory.

```
mdmdoc ui                         # operator web console (drag-drop, review, training, debug)
mdmdoc check-bank <pdf|image> [--sap <screenshot>]   # verdict + fields (+ SAP comparison)
mdmdoc check-w9   <pdf|image>     # W-9 vs W-8, Line 1/2, classification, masked TIN
mdmdoc review last --open         # correct a result -> labeled example (the teach loop)
mdmdoc train --fewshot            # fold corrections into the prompts
mdmdoc eval --tag after-fewshot   # measure before/after
mdmdoc serve --api-only           # headless REST API (the BTP surface)
mdmdoc runs | mdmdoc doctor
```

**Operator console** (`mdmdoc ui`, Codex-look, no build step): Dashboard
(drag-drop checks with live progress, optional SAP screenshot), run pages with
document preview + EXTRACTED DATA + findings + SAP comparison table, a review
form that turns corrections into training examples, Training (few-shot /
Modelfile / eval with metric sparklines), a **Rules** editor, and Debug pages.

**Local companion to the ABAP/SAP validator.** This console is optional and runs
locally, in parallel with SAP — it is where the *analyses* and *training* live
(the ABAP clone [`mdm-doc-validator-abap`](../mdm-doc-validator-abap) does the
in-MDG deterministic validation). The two connect two ways:
- **Same report schema** `mdmdoc.v1`: the ABAP report `ZMDMDOC` exports JSON
  (`cb_json`) that opens on the run pages here, so a SAP-side check is reviewable
  in the console.
- **Same rules**: the **Rules** page (`/ui/rules`) edits `rules/*.yaml` — the model
  never decides verdicts, only these rules do. Save writes the YAML; **Regenerate
  for SAP** copies them into the ABAP repo and runs its generator
  (`MDMDOC_ABAP_HOME`, default `~/Projects/mdm-doc-validator-abap`), so a rule you
  change locally reaches SAP. Delete a rule by removing its block; replace a whole
  "skill" (banking / W-9) by pasting a new file.

**SAP BTP packaging**: the same API ships as an api-only Docker image with an
OpenAPI contract and CF/Kyma artifacts — see [`btp/`](btp/) and
[docs/BTP_INTEGRATION.md](docs/BTP_INTEGRATION.md).

| doc | what |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, components, data lifecycle |
| [docs/API.md](docs/API.md) | REST reference + mdmdoc.v1 schema |
| [docs/BTP_INTEGRATION.md](docs/BTP_INTEGRATION.md) | Docker/CF/Kyma, model topologies, auth |
| [docs/PRIVACY.md](docs/PRIVACY.md) | masking model, leak gate, erasure |
| [docs/OPERATOR_GUIDE_RU.md](docs/OPERATOR_GUIDE_RU.md) | руководство оператора (RU) |
| [docs/SKILL_SYNC.md](docs/SKILL_SYNC.md) | sync checker skills (mdm-w9-checker …) into the validator |
| [TRAINING.md](TRAINING.md) | label → few-shot → LoRA ladder |

Exit codes: `0` ACCEPT · `1` REJECT · `2` WARNING/NEED_MANUAL_REVIEW · `3` Ollama not
running · `4` unreadable input.

## Built to find the data, not just read page 1

- **Multi-page**: every page (up to 12) gets a cheap survey read; pages are scored by
  banking/W-9 keyword + regex density and only the best pages get the expensive
  300-DPI OCR + vision treatment. A statement with details on page 7 works.
- **Sideways photos**: tesseract OSD detects orientation (confidence-gated) with a
  brute-force 90/180/270 fallback; the detected rotation is applied to both OCR and
  vision renders.
- **Escalation**: if a bank document shows no account identifiers after the first
  pass, a second *targeted* vision pass hunts specifically for payment details.
- **Languages**: text layer for anything digital; tesseract with CJK retry; vision
  model for ES/DE/ZH/KO/RU scans; account-number keywords cover
  EN/ES/DE/FR/PT/RU/KO/JA/ZH (cuenta, Konto, compte, счёт, 계좌, 口座, 账户…).
- **Non-obvious layouts**: that's what the teach loop is for — one `review` turns a
  missed document into a few-shot exemplar.

## How it works

```
document ──► Stage A (frozen): text layer / tesseract 300dpi / qwen2.5vl transcription
                               + deterministic regex (IBAN/SWIFT/EIN/SSN/routing/account)
        ──► Stage B (trainable): qwen3:4b text→JSON {doc_type, fields}, few-shot injected
                               + OCR cross-check overlay (regex beats the model on IDs)
        ──► rules/*.yaml (explicit, editable) ──► verdict + report
```

- **The model only classifies and extracts.** Verdicts come from `rules/banking.yaml` /
  `rules/w9.yaml` — readable, editable, no hidden intuition.
- **Privacy choke point** (`privacy.py`): masked display everywhere, `scrub_text` on any
  persisted text, `assert_no_leak` gate on every artifact write, leakage metric in eval
  must be 0. Few-shot/LoRA exemplars use shape-preserving fakes, never real values.
- **Inference runs on the Mac mini by default.** The mini's Ollama binds 127.0.0.1, so
  mdmdoc auto-opens an SSH tunnel (`ssh -f -N -L 11435:127.0.0.1:11434 mac-mini`) and
  talks to it at `http://127.0.0.1:11435`. Override with `MDMDOC_OLLAMA_HOST` (e.g.
  `http://localhost:11434` to force the local MacBook Ollama). No Ollama server is ever
  auto-started — the tool only tunnels to one that is already running; models are called
  sequentially with `keep_alive=0` (fits the mini's OLLAMA_MAX_LOADED_MODELS=1).

## Setup (one time)

```bash
cd ~/Projects/mdm-doc-validator
uv tool install --editable .             # puts `mdmdoc` on your PATH, runs from anywhere
brew install tesseract tesseract-lang    # if missing
mdmdoc doctor                            # checks mini tunnel, models, tesseract, dirs
```

After this, just use `mdmdoc ...` from any folder — no `cd`, no `uv run`.
Re-run `uv tool install --editable . --force` only if you move the project folder;
code edits take effect immediately (editable install).

**Paths with spaces** are handled — quotes are optional, and you can type
`mdmdoc check-bank ` (trailing space) then **drag the file from Finder** into the
terminal:

```bash
mdmdoc check-bank ~/Desktop/documents/banking/eu/some bank letter.pdf
mdmdoc check-w9 "~/Desktop/documents/w9/Acme W-9.pdf"
```

Model roles (env-overridable): `MDMDOC_VISION` (qwen2.5vl:7b), `MDMDOC_TEXT` (qwen3:4b),
`MDMDOC_EMBED` (nomic-embed-text).

## Training

See [TRAINING.md](TRAINING.md): label → few-shot → (at 100+ labels) MLX LoRA.

## Layout

- `src/mdmdoc/` — pipeline (`stage_a`, `stage_b`, `rules/engine`, `verdict`, `report`,
  `privacy`, teach-loop: `review`, `fewshot`, `evalrun`, `lora_export`)
- `rules/` — the actual compliance rules (edit these, not the model)
- `prompts/` — system prompts + generated few-shot exemplars
- `dataset/labels.jsonl` — masked labeled examples (committable)
- `runs/<sha16>/` — per-document artifacts (gitignored)
- `compare.py` — v2 stub: vendor-template comparison hooks in via extra findings
