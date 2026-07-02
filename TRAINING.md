# Training the Stage-B model

The pipeline has two stages. Stage A (OCR/vision transcription + deterministic regex)
is frozen — it is not trained. Stage B (text → structured JSON: doc type + fields) is
the trainable part, and there are three escalating ways to improve it. **Rules are never
trained** — verdict logic stays in `rules/*.yaml`.

## Level 0 — label (always do this)

Every time a check gets something wrong:

```bash
mdmdoc review last --open      # correct the fields / doc type / verdict
```

Corrections land in `dataset/labels.jsonl` (masked — full tax IDs / account numbers
never enter the dataset; sensitive values are stored as masked + derived facts +
shape-preserving fakes). Then measure:

```bash
mdmdoc eval --tag baseline
```

## Level 1 — few-shot (works from ~5 labels)

```bash
mdmdoc train --fewshot         # picks the most instructive labels per doc type
mdmdoc eval --tag after-fewshot
```

`eval/report.md` shows the before/after delta. Exemplars use fake values with real
shape (`DE89…`, `XX-XXX…`), so the model learns to copy exact-looking strings without
any real value leaving memory.

Optionally package prompts + exemplars into a named Ollama model:

```bash
mdmdoc train --modelfile --apply    # builds `mdmdoc-extract` (ollama must be running)
export MDMDOC_TEXT=mdmdoc-extract
```

## Level 2 — LoRA fine-tune (only at 100+ labels)

A LoRA on a handful of examples overfits, so `export-lora` is gated:

```bash
mdmdoc export-lora            # refuses under 100 labels (--force only to test the format)
```

This writes `dataset/mlx-lora/{train,valid}.jsonl` in mlx-lm chat format
(system/user/assistant, fake-shape values only). Then:

```bash
# 1. tooling (one-time)
uv add mlx-lm

# 2. train the adapter (Qwen3-4B ≈ the Ollama qwen3:4b Stage-B default)
uv run mlx_lm.lora --model Qwen/Qwen3-4B --train \
    --data dataset/mlx-lora --iters 600 --batch-size 2 --num-layers 8

# 3. fuse the adapter into the weights
uv run mlx_lm.fuse --model Qwen/Qwen3-4B --adapter-path adapters \
    --save-path fused-mdmdoc

# 4. convert to GGUF for Ollama (llama.cpp checkout required)
python /path/to/llama.cpp/convert_hf_to_gguf.py fused-mdmdoc \
    --outfile mdmdoc-extract-lora.gguf --outtype q4_k_m

# 5. register with Ollama (server already running — never auto-start it)
printf 'FROM ./mdmdoc-extract-lora.gguf\nPARAMETER temperature 0.1\n' > models/Modelfile.lora
ollama create mdmdoc-extract-lora -f models/Modelfile.lora
export MDMDOC_TEXT=mdmdoc-extract-lora

# 6. ALWAYS eval before adopting
mdmdoc eval --tag after-lora
```

Adopt the LoRA model only if `eval` improves over the few-shot baseline and
`leakage_count` stays 0.

## What the metrics mean

| metric | target |
|---|---|
| doc_type_accuracy | ↑; invoices must never classify as bank_letter |
| invoice_false_accept_rate | **0** — an accepted invoice is the worst failure |
| per-field exact match | ↑ per field; IBAN/SWIFT driven by OCR regex, so failures there usually mean Stage A problems |
| json_valid_first_try | ↑; drops mean the prompt/model regressed |
| leakage_count | **hard 0** — eval exits non-zero otherwise |
