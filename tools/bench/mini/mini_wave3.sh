#!/bin/bash
# Wave 3: iteration combos (tiles / ocrhint / twopass) on core, after waves 1–2.
# Run:  queue bench3 bench/mini_wave3.sh
. "$(dirname "$0")/lib.sh"
wait_for_session_end bench bench2
banner "wave 3 (combos) on core"
C1="ollama:qwen2.5vl:7b@v170#transcribe_md~tiles:q4,ollama:qwen2.5vl:7b@v170#transcribe_md~ocrhint,ollama:qwen2.5vl:7b@v170#transcribe_md~tiles:q4~ocrhint,ollama:qwen2.5vl:7b@v170#transcribe_md~twopass"
C2="ollama:qwen3-vl:8b@v200#transcribe_md~tiles:q4,ollama:qwen3-vl:8b@v200#transcribe_md~ocrhint,ollama:qwen3-vl:4b@v200#transcribe_md~tiles:q4"
$B run --engines "$C1,$C2" --docs tag:core --tag w3
banner "wave 3 on handwriting (public)"
$B run --engines "ollama:qwen2.5vl:7b@v170#transcribe_md~tiles:q4,ollama:qwen3-vl:8b@v200#transcribe_md~tiles:q4" --docs stratum:public --tag w3
banner "DONE wave3"
