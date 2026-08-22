#!/bin/bash
# Second OCR voice (RapidOCR, CPU) over the whole corpus — the third family for the
# consensus layer. CPU-only, so it may run alongside an Ollama session.
# Run:  queue rapid bench/mini_rapidocr.sh
. "$(dirname "$0")/lib.sh"
uv sync --group bench 2>&1 | tail -1
banner "rapidocr over the corpus"
$B run --engines "rapidocr:auto" --docs all --tag w1
banner "DONE rapidocr"
