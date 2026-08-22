#!/bin/bash
# Shared helpers for the benchmark scripts that run on the Mac mini.
# Source it:  . "$(dirname "$0")/lib.sh"
#
# WHY THIS FILE EXISTS: `tmux has-session -t bench` matches any session whose name
# STARTS with "bench" — including "bench2". Wave 2 waited for the "bench" session to
# end while running inside "bench2", i.e. it waited for itself, for 18 hours. Every
# session test below uses the exact-match form `=NAME`; never write `-t NAME` again.

cd ~/Projects/mdm-doc-validator-bench || exit 1
export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH
B="uv run mdmdoc bench"

session_alive() {            # session_alive NAME → 0 if a tmux session named EXACTLY NAME exists
  tmux has-session -t "=$1" 2>/dev/null
}

wait_for_session_end() {     # wait_for_session_end NAME [NAME…] — block while ANY of them is alive
  local n
  while :; do
    local any=0
    for n in "$@"; do session_alive "$n" && any=1; done
    [ "$any" = 0 ] && return 0
    sleep 60
  done
}

queue() {                    # queue NAME SCRIPT [LOG] — start SCRIPT in a detached tmux session NAME
  local name="$1" script="$2" log="${3:-bench/logs/$1.log}"
  if session_alive "$name"; then
    echo "REFUSED: tmux session '$name' already exists — pick another name or wait" >&2
    return 1
  fi
  mkdir -p "$(dirname "$log")"
  tmux new-session -d -s "$name" "bash '$script' >> '$log' 2>&1"
  echo "queued '$name' → $log"
}

banner() { echo "=== $(date) $*"; }
