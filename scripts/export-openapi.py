#!/usr/bin/env python3
"""Export the api-only OpenAPI contract to btp/openapi.json (no server start,
no model-host contact)."""
import json
import os
import sys
from pathlib import Path

os.environ["MDMDOC_MODE"] = "api-only"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdmdoc.server.app import create_app  # noqa: E402

out = Path(__file__).resolve().parents[1] / "btp" / "openapi.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(create_app("api-only").openapi(), indent=2, ensure_ascii=False))
print(f"wrote {out}")
