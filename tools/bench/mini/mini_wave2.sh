#!/bin/bash
# Wave 2 (re-scoped 2026-08-21 after Egor's constraint: the extractor must run on a
# Windows work machine and later plug into SAP via the ABAP twin ZMDMDOC).
# ZMDMDOC has only its own PDF text layer + an Ollama HTTP client — so ONLY Ollama
# vision models can win. MLX (Apple Silicon) and Apple Vision (macOS) are out.
# Run:  queue bench2 bench/mini_wave2.sh
. "$(dirname "$0")/lib.sh"
wait_for_session_end bench
banner "wave 2 (Ollama-only) starts"
for m in minicpm-v:8b granite3.2-vision:2b; do echo "--- pull $m"; ollama pull "$m" 2>&1 | tail -1; done
OLL="ollama:qwen3-vl:8b@v200#transcribe_md,ollama:qwen3-vl:4b@v200#transcribe_md,ollama:deepseek-ocr:3b@v200,ollama:minicpm-v:8b@v200#transcribe_md,ollama:granite3.2-vision:2b@v200#transcribe_md"
banner "wave 2 on core (canaries)"
$B run --engines "$OLL" --docs tag:core --tag w2
banner "wave 2 on handwriting (public)"
$B run --engines "$OLL" --docs stratum:public --tag w2
banner "wave-1 VLM on public"
$B run --engines "ollama:qwen2.5vl:7b@v170#transcribe_md" --docs stratum:public --tag w1
$B run --engines "textlayer,tess:auto" --docs stratum:public --tag w1
banner "doc-type"
$B doctype --tag w1
$B doctype --tag w2
banner "DONE wave2"
