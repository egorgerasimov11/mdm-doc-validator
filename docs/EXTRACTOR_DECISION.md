# Extractor benchmark — decision (2026-08-22)

**Question.** Can a local engine (OCR / vision-language model) transcribe any vendor
document — every character, every language, handwriting included — well enough to
replace the validator with a pure extractor, running on a Windows work machine and
later inside SAP through the ABAP twin ZMDMDOC, with the user waiting **at most one
minute per document**?

**Verdict: HYBRID.** No local engine meets the bar. The PDF text layer, guarded by
the plausibility gate that already exists on both sides (Python `plausibility.py`,
ABAP `ZCL_MDMDOC_PDF`), is exact and instant for digital documents and goes in now.
Scans, photos and handwriting cannot be extracted locally within the quality and
time budget and must go to a human queue or to a remote model (Claude produced the
gold standard at ~$1/page and is the only engine that reached 100 %).

## Evidence (wave 1, 66 real documents, 116 gold pages, target platform `windows`)

Gold: Claude Agent SDK, two passes per page, median pass-to-pass CER 0. Metric:
every gold label→value must appear in the transcript (order-insensitive);
*worst-doc* = the weakest document in the slice. Time = whole document (mean page
latency × page count), on an Apple-silicon Mac mini with 16 GB — a GPU-less Windows
laptop will not be faster.

| engine | field recall worst · macro | doc time median · p90 · ≤60 s | verdict |
|---|---|---|---|
| `ollama:qwen2.5vl:7b@v200` (best local VLM) | 50.0 % · 92.7 % | 66 s · 775 s · 42 % | fails quality and time |
| `layer>qwen2.5vl:7b` (text layer when plausible, else VLM) | 16.7 % · 91.0 % | 28 s · 114 s · 74 % | time still fails; quality bounded by VLM on scans |
| `layer>tess:auto` | 0.0 % · 78.1 % | 1 s · 8 s · 99 % | fast, loses values |
| `textlayer` alone | 0.0 % · 43.4 % | 0 s · 0 s · 100 % | exact where present, nothing on scans |
| gemma3:4b, qwen3-vl 4b/8b, deepseek-ocr, minicpm-v, granite3.2-vision | worse or timed out (300 s/page at 16 GB) | | out |

Thresholds (`src/mdmdoc/bench/metrics.py`): worst-doc field recall 100 %, entity
99.5 %, CER ≤ 1 %, document p90 ≤ 60 s (handwriting 95 % / 90 s). Nothing local
passes any print slice.

Other findings that shaped the verdict:

- **VLM loops.** 3–10 % of pages from every local VLM degenerated into a repeated
  cell until the token limit (one French RIB: 279 s, zero values). Fixed with
  `repeat_penalty` + a structural loop detector and tile re-read
  (`src/mdmdoc/extract/loops.py`); the canary now reads in 72 s with every value —
  but that removes garbage, it does not make the model faster or complete.
- **Prompt.** The shared transcription prompt (now `ZCL_MDMDOC_PROMPTS`) beats the
  legacy ABAP prompt on the canaries (4 vs 6 errors, 106 s vs 116 s median).
- **Handwriting.** Worst-doc recall 50 % on public handwriting sets; not usable
  without a human.
- **Multi-page.** 38 of 66 real documents exceed one minute on the best VLM;
  a 42-page packet extrapolates to 36 minutes.

## What this means for ZMDMDOC

1. Keep `p_ovis` as documented; the vision path is a fallback, never the automatic
   path for the 60-s promise.
2. Trust the text layer only through `layer_usable` (already ported and pinned).
3. When the layer is unusable (scan, photo, garbage OCR layer): flag for a human or a
   remote transcription service — do not block the user on a local model.
4. Re-run the benchmark on the actual work machine if it ever gets a GPU:
   `uv run mdmdoc bench run --engines ollama:qwen2.5vl:7b@v200#transcribe_md --docs tag:core --tag work-gpu`
   then `bench report --tag work-gpu --decide --platform windows`.

## Where the data lives

`bench/` is git-ignored on purpose (full, unmasked document values): corpus
manifest, gold, result cells, `leaderboard.md`, generated `DECISION.md`. The
benchmark code is in `src/mdmdoc/bench/`, the sweep scripts for the Mac mini in
`tools/bench/mini/`. A partial re-run after the loop fix (`loopfix`) is still
completing on the mini; its first 8 documents agree with the table above.

## Not done on purpose

- Phase 2 — `mdmdoc extract` as a user-facing command and a Python service that
  takes scans from SAP — waits for an explicit go.
- Wave 3 (tile / OCR-hint / two-pass combinations) was not run: the combos slow the
  engine further and the time budget is already the binding constraint.
