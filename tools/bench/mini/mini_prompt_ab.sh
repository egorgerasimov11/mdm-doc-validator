#!/bin/bash
# A/B of the vision prompt on the canaries: the text ZMDMDOC used to carry vs the
# shared one. Same model, same render — only the prompt differs.
# Run:  queue promptab bench/mini_prompt_ab.sh
. "$(dirname "$0")/lib.sh"
wait_for_session_end bench bench2
banner "prompt A/B on canaries"
$B run \
  --engines "ollama:qwen2.5vl:7b@v170#abap_legacy.v1,ollama:qwen2.5vl:7b@v170#transcribe_md.v1" \
  --docs tag:core --tag prompt-ab
banner "DONE prompt A/B"
