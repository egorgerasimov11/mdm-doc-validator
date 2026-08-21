#!/usr/bin/env python3
"""MLX-VLM worker: load one model, then answer JSONL requests on stdin.

    python worker.py --model mlx-community/Qwen3-VL-8B-Instruct-4bit
    → {"ready": true, "model": ...}
    ← {"images": ["/abs/page.jpg"], "prompt": "...", "max_tokens": 4096}
    → {"text": "...", "latency_s": 12.3, "gen_tokens": 812, "prompt_tokens": 1500,
       "peak_mem_gb": 6.1, "truncated": false}

    --check          exit 0 if the weights are in the HF cache (no load)
    --download-only  download the snapshot and exit

Spawned by mdmdoc.extract.engines.MLXVLMEngine in its own process group; it
never opens ports and dies with its stdin.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--download-only", action="store_true")
    a = ap.parse_args()

    if a.check or a.download_only:
        from huggingface_hub import snapshot_download
        try:
            path = snapshot_download(a.model, local_files_only=a.check)
        except Exception as e:
            log(f"{'not cached' if a.check else 'download failed'}: {e}")
            return 1
        print(path)
        return 0

    import mlx.core as mx
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config

    t0 = time.time()
    model, processor = load(a.model)
    config = load_config(a.model)
    print(json.dumps({"ready": True, "model": a.model, "load_s": round(time.time() - t0, 1)}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            images = req["images"]
            prompt = req["prompt"]
            max_tokens = int(req.get("max_tokens", 4096))
            formatted = apply_chat_template(processor, config, prompt, num_images=len(images))
            t1 = time.time()
            mx.reset_peak_memory() if hasattr(mx, "reset_peak_memory") else None
            try:
                out = generate(model, processor, formatted, image=images, max_tokens=max_tokens,
                               temperature=0.0, verbose=False)
            except TypeError:
                out = generate(model, processor, formatted, images, max_tokens=max_tokens,
                               temperature=0.0, verbose=False)
            if isinstance(out, str):
                text, gen_tokens, prompt_tokens = out, None, None
            else:
                text = getattr(out, "text", str(out))
                gen_tokens = getattr(out, "generation_tokens", None)
                prompt_tokens = getattr(out, "prompt_tokens", None)
            peak = None
            try:
                peak = round(mx.get_peak_memory() / 1e9, 2)
            except Exception:
                pass
            print(json.dumps({"text": text, "latency_s": round(time.time() - t1, 2),
                              "gen_tokens": gen_tokens, "prompt_tokens": prompt_tokens,
                              "peak_mem_gb": peak,
                              "truncated": bool(gen_tokens is not None and gen_tokens >= max_tokens)},
                             ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"error": f"{e.__class__.__name__}: {e}",
                              "trace": traceback.format_exc()[-800:]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
