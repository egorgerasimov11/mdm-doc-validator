#!/bin/bash
# Re-run the wave-1 vision engines after the loop-recovery change (engine versions
# now carry the generation options, so only VLM cells are redone; textlayer /
# tesseract / Apple Vision cells are kept). Starts with the French RIB that looped.
# Run:  queue loopfix bench/mini_loopfix.sh
. "$(dirname "$0")/lib.sh"
wait_for_session_end bench bench2 bench3 promptab catchup
VLM="ollama:qwen2.5vl:7b@v200#transcribe_md,ollama:qwen2.5vl:7b@v170#transcribe_md,ollama:gemma3:4b@v170#transcribe_md"
banner "loop-fix canary: RIB_ATREEC"
$B run --engines "ollama:qwen2.5vl:7b@v200#transcribe_md" --docs id:fa294d0e4772f2d8 --tag w1 --force
banner "loop-fix: wave-1 VLM over the real corpus (new engine version → cells redone)"
$B run --engines "$VLM" --docs stratum:real --tag w1
$B run --engines "ollama:qwen2.5vl:7b@v170#transcribe_md" --docs stratum:public --tag w1
banner "loop-fix: wave-2 engines (cells written by the pre-fix code are version-mismatched and redone)"
OLL="ollama:qwen3-vl:4b@v200#transcribe_md,ollama:deepseek-ocr:3b@v200,ollama:minicpm-v:8b@v200#transcribe_md,ollama:granite3.2-vision:2b@v200#transcribe_md"
$B run --engines "$OLL" --docs tag:core --tag w2
$B run --engines "$OLL" --docs stratum:public --tag w2
$B doctype --tag w1
$B doctype --tag w2
banner "DONE loopfix"
