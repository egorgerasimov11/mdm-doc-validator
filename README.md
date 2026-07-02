# mdm-doc-validator

Local, privacy-safe CLI that validates **banking support documents** and **US W-9/W-8
tax forms** for SAP MDM work. Everything runs on-device (Ollama + tesseract); documents
never leave the machine, and full tax IDs / account numbers never leave process memory.

```
mdmdoc check-bank <pdf|image>     # bank letter / statement / screenshot / invoice? -> verdict + fields
mdmdoc check-w9   <pdf|image>     # W-9 vs W-8, Line 1/2, classification, masked TIN -> verdict + fields
mdmdoc review last --open         # correct a result -> labeled example (the teach loop)
mdmdoc train --fewshot            # fold corrections into the prompts
mdmdoc eval --tag after-fewshot   # measure before/after
mdmdoc runs | mdmdoc doctor
```

Exit codes: `0` ACCEPT · `1` REJECT · `2` WARNING/NEED_MANUAL_REVIEW · `3` Ollama not
running · `4` unreadable input.

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

## Setup

```bash
uv sync
brew install tesseract tesseract-lang   # if missing
mdmdoc doctor                            # checks ollama, models, tesseract, dirs
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
