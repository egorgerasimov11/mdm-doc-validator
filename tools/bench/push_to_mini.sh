#!/bin/bash
# Push CODE to the Mac mini bench checkout. Direction is one-way by design:
#   MacBook → mini : src/, prompts/, tools/bench/mini/*.sh   (this script)
#   mini → MacBook : bench/results/, bench/logs/              (bench/sync_from_mini.sh)
# The mini is never edited by hand, so --delete on src/ and prompts/ is safe; bench/
# on the mini also holds results, logs and the corpus, so scripts are copied there
# WITHOUT --delete.
# ssh swallows exit codes of remote pipelines — success is the READY marker on stdout.
set -u
cd "$(dirname "$0")/../.." || exit 1
M=mac-mini
R=/Users/victor/Projects/mdm-doc-validator-bench
rsync -az --delete src/ "$M:$R/src/" || { echo "rsync src FAILED"; exit 1; }
rsync -az --delete prompts/ "$M:$R/prompts/" || { echo "rsync prompts FAILED"; exit 1; }
rsync -az tools/bench/mini/ "$M:$R/bench/" || { echo "rsync scripts FAILED"; exit 1; }
out=$(ssh -n "$M" "cd $R && export PATH=\$HOME/.local/bin:/opt/homebrew/bin:\$PATH && \
  uv run python -c 'import mdmdoc.extract.engines as E, mdmdoc.extract.loops; print(E.parse(\"ollama:qwen2.5vl:7b\").version)' && \
  bash -n bench/lib.sh bench/mini_*.sh && grep -c 'has-session -t \"=' bench/lib.sh && printf 'READY\n'" 2>&1)
echo "$out"
case "$out" in *READY*) echo "mini READY";; *) echo "mini NOT READY"; exit 1;; esac
