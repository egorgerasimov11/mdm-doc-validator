#!/usr/bin/env python3
"""
modelfile.py — package the current prompts (+ a couple of few-shot exemplars)
into a custom Ollama model `mdmdoc-extract`, the same way sap-mdm-smart/fast
are built. `--apply` runs `ollama create` against the ALREADY RUNNING server —
this tool never starts Ollama.
"""
from __future__ import annotations

import json
import subprocess

from . import config, model_client as mc

MODEL_NAME = "mdmdoc-extract"
CANDIDATE_NAME = "mdmdoc-extract-candidate"   # adoption gate builds land here first
STOCK_BASE = "qwen3:4b"


def _fewshot_messages(doc_class: str, limit: int = 2) -> list[str]:
    p = config.FEWSHOT_DIR / f"{doc_class}.json"
    if not p.exists():
        return []
    try:
        examples = json.loads(p.read_text())[:limit]
    except Exception:
        return []
    out = []
    for ex in examples:
        out.append('MESSAGE user """' + ex.get("input", "") + '"""')
        out.append('MESSAGE assistant """' + json.dumps(ex.get("output", {}), ensure_ascii=False) + '"""')
    return out


def build_modelfile(apply: bool = False, name: str = MODEL_NAME) -> int:
    config.ensure_dirs()
    # base is always the STOCK model — never our own builds (would self-stack)
    base = mc.ROLES["TEXT"] if mc.ROLES["TEXT"] not in (MODEL_NAME, CANDIDATE_NAME) else STOCK_BASE
    sys_bank = (config.PROMPTS_DIR / "system_bank.txt").read_text()
    sys_w9 = (config.PROMPTS_DIR / "system_w9.txt").read_text()
    system = (sys_bank + "\n\n--- When the input is a US tax form instead of a banking document: ---\n\n"
              + sys_w9)
    lines = [f"FROM {base}",
             "PARAMETER temperature 0.1",
             'SYSTEM """' + system + '"""']
    lines += _fewshot_messages("bank") + _fewshot_messages("w9")
    mf = config.MODELS_DIR / f"Modelfile.{name}"
    mf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {mf} (FROM {base})")
    cmd = ["ollama", "create", name, "-f", str(mf)]
    if not apply:
        print("to build the model (with Ollama already running):")
        print("  " + " ".join(cmd))
        print(f"then use it: export MDMDOC_TEXT={name}")
        return 0
    rc = ollama_cmd(cmd)
    if rc == 0:
        print(f"model {name} created. Use it: export MDMDOC_TEXT={name}")
    return rc


def ollama_cmd(cmd: list[str]) -> int:
    """Run an `ollama ...` CLI command against the resolved endpoint (mini
    tunnel). Never starts a server."""
    try:
        mc.preflight()
    except mc.OllamaUnavailable as e:
        print(str(e))
        return config.EXIT_OLLAMA_DOWN
    import os
    env = dict(os.environ, OLLAMA_HOST=mc.host())
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(build_modelfile())
