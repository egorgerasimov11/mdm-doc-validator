"""Document-transcription benchmark.

Gold transcripts come from Claude (Agent SDK); candidates are local engines
(OCR, local vision models, combinations). Everything the benchmark writes
lives under config.BENCH_DIR (gitignored) — full, unmasked document text.
"""
