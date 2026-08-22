#!/bin/bash
# Wave 1: free engines over the whole corpus, then the first vision models.
# Run:  queue bench bench/mini_wave1.sh     (from lib.sh)
. "$(dirname "$0")/lib.sh"
banner "free engines over the whole corpus"
$B run --engines "textlayer,tess:auto,applevision:legacy,applevision:document,applevision:legacy~nocorrect,applevision:document~nocorrect" --docs all --tag w1
banner "pulling vision models"
ollama pull qwen2.5vl:7b 2>&1 | tail -1
ollama pull gemma3:4b 2>&1 | tail -1
VLM="ollama:qwen2.5vl:7b@v170#transcribe_md,ollama:qwen2.5vl:7b@v200#transcribe_md,ollama:gemma3:4b@v170#transcribe_md"
banner "VLM on core"
$B run --engines "$VLM" --docs tag:core --tag w1
banner "VLM on the real corpus"
$B run --engines "$VLM" --docs stratum:real --tag w1
banner "DONE wave1"
