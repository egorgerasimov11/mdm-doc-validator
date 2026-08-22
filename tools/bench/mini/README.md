# Benchmark scripts for the Mac mini

These run the sweeps on `mac-mini` (`~/Projects/mdm-doc-validator-bench`). They are
the source of truth — the copies on the mini are pushed from here, never edited there.

## Invariants (each one was paid for)

- **tmux session names are matched exactly: `tmux has-session -t "=NAME"`.** The
  bare `-t NAME` form is a prefix match: `-t bench` also matches `bench2`, so wave 2
  waited for itself for 18 hours. Use `session_alive`, `wait_for_session_end` and
  `queue` from `lib.sh`; never call `tmux has-session` directly.
- **`queue NAME SCRIPT` refuses to start if a session with that exact name exists.**
- **One model resident at a time.** The mini has 16 GB; every sweep runs engines
  sequentially (setup → pages → teardown) and waits for the earlier session(s).
  Models above ~8 GB swap and time out (qwen3-vl:8b hit 300 s per page).
- **Watch the disk.** `df -h /` before pulling a model; 23 GB of MLX weights once
  filled it. MLX and Apple Vision are out of scope anyway (target = Windows + SAP).
- **Sync directions.** `tools/bench/push_to_mini.sh` pushes `src/`, `prompts/` and
  these scripts (MacBook → mini). `bench/sync_from_mini.sh` pulls `bench/results/`
  and logs (mini → MacBook). Never the other way round; gold lives on the MacBook.
- **READY protocol.** `ssh` swallows the exit status of remote pipelines, so every
  remote step prints `READY` on success and the caller greps for it.
- **Engine versions.** `CODE_VERSION` is bumped only for code changes that alter
  every family; generation options are hashed into the VLM versions, so changing
  `repeat_penalty` invalidates Ollama cells and nothing else. `mdmdoc bench status`
  shows `⚠ mixed` when an engine directory holds two versions; the report scores
  only the newest and warns about the stale cells.
- Never `git add -A` in either repo; `bench/` and `doct/` are gitignored on purpose
  (full, unmasked document values).

## Usage

```bash
tools/bench/push_to_mini.sh                          # prints "mini READY"
ssh -n mac-mini '. ~/Projects/mdm-doc-validator-bench/bench/lib.sh; queue loopfix bench/mini_loopfix.sh'
ssh -n mac-mini 'tmux ls; tail -3 ~/Projects/mdm-doc-validator-bench/bench/logs/loopfix.log'
bench/sync_from_mini.sh && uv run mdmdoc bench report --tag w1 --decide --no-diffs --platform windows
```

| script | session | what |
|---|---|---|
| `mini_wave1.sh` | `bench` | free engines on all docs, qwen2.5vl/gemma3 on real |
| `mini_wave2.sh` | `bench2` | Ollama-only candidates (qwen3-vl, deepseek-ocr, minicpm-v, granite) |
| `mini_wave3.sh` | `bench3` | tiles / ocrhint / twopass combos on core |
| `mini_prompt_ab.sh` | `promptab` | ABAP legacy prompt vs shared prompt |
| `mini_catchup.sh` | `catchup` | documents added after a stage finished (`NEW=id:…`) |
| `mini_loopfix.sh` | `loopfix` | re-run wave-1 VLM after the loop-recovery change |
| `build_corpus.sh` | — | corpus materialisation on the mini |
