#!/bin/bash
# Catch-up for documents Egor added after a stage had already finished.
# Run:  queue catchup bench/mini_catchup.sh
. "$(dirname "$0")/lib.sh"
NEW="${NEW:-id:96fb8538edf6a7ba,id:17e088e92d711548,id:aac770755d11dd98}"
banner "catch-up: free engines"
$B run --engines "textlayer,tess:auto,applevision:legacy,applevision:document" --docs "$NEW" --tag w1
wait_for_session_end bench bench2 bench3
banner "catch-up: wave-1 VLM"
$B run --engines "ollama:qwen2.5vl:7b@v170#transcribe_md,ollama:qwen2.5vl:7b@v200#transcribe_md" --docs "$NEW" --tag w1
banner "DONE catch-up"
