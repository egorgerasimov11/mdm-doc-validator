"""casestore.py — persistent state of consolidation cases.

A case = one operator session: extracted vendors + the uploaded template +
versioned outputs, under runs/consolidation/<case_id>/. Outputs are never
overwritten: every consolidate saves consolidated_vNNN.xlsx, and the newest
VERIFIED one is what the download route serves. All JSON goes through
config.atomic_write_text (crash-safe replace, house style).
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from .. import config

_CASE_ID_RE = re.compile(r"^c\d{8}_\d{6}_[0-9a-f]{4}$")
_VKEY_RE = re.compile(r"^v\d{2}$")
_OUT_NAME_RE = re.compile(r"^consolidated_v\d{3}\.xlsx$")


def consol_dir() -> Path:
    # computed per call so tests can monkeypatch config.RUNS_DIR
    return Path(config.RUNS_DIR) / "consolidation"


def bp_template_default() -> Path | None:
    """Optional blank BusinessPartnerTemplate for starting without an upload.
    Env override wins; else templates/consolidation/ (dropped there by hand —
    the SAP-delivered artifact is not generatable and stays gitignored)."""
    import os
    p = os.environ.get("MDMDOC_BP_TEMPLATE", "").strip()
    if p:
        path = Path(p).expanduser()
        return path if path.exists() else None
    path = Path(config.TEMPLATES_DIR) / "consolidation" / "BusinessPartnerTemplate.xlsx"
    return path if path.exists() else None


def case_dir(case_id: str) -> Path:
    if not _CASE_ID_RE.fullmatch(case_id):
        raise ValueError(f"bad case id {case_id!r}")
    return consol_dir() / case_id


def new_case() -> dict:
    case_id = (
        "c" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
    )
    case = {
        "case_id": case_id,
        "created": datetime.now().isoformat(timespec="seconds"),
        "vendors": [],
        "template": None,
        "outputs": [],
    }
    save_case(case)
    return case


def _gated_write(path: Path, payload: dict) -> None:
    """Case JSON goes through the leak gate like run artifacts: TIN values are
    already masked at build time (convert/docrows coverage), the gate proves
    it on every write. Full values live only in the output workbook."""
    from ..privacy import assert_no_leak
    blob = json.dumps(payload, ensure_ascii=False, indent=1)
    assert_no_leak(blob, [], policy=config.gate_policy())
    config.atomic_write_text(path, blob)


def save_case(case: dict) -> None:
    d = case_dir(case["case_id"])
    d.mkdir(parents=True, exist_ok=True)
    _gated_write(d / "case.json", case)


def load_case(case_id: str) -> dict | None:
    p = case_dir(case_id) / "case.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def next_vkey(case: dict) -> str:
    return f"v{len(case['vendors']) + 1:02d}"


def save_extract(case_id: str, vkey: str, data: dict) -> None:
    if not _VKEY_RE.fullmatch(vkey):
        raise ValueError(f"bad vendor key {vkey!r}")
    d = case_dir(case_id)
    d.mkdir(parents=True, exist_ok=True)
    _gated_write(d / f"extract_{vkey}.json", data)


def load_extract(case_id: str, vkey: str) -> dict | None:
    if not _VKEY_RE.fullmatch(vkey):
        return None
    p = case_dir(case_id) / f"extract_{vkey}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def out_dir(case_id: str) -> Path:
    d = case_dir(case_id) / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def next_output_name(case: dict) -> str:
    return f"consolidated_v{len(case['outputs']) + 1:03d}.xlsx"


def output_path(case_id: str, name: str) -> Path | None:
    """Traversal-guarded path of one output file."""
    if not _OUT_NAME_RE.fullmatch(name):
        return None
    p = (out_dir(case_id) / name).resolve()
    if p.parent != out_dir(case_id).resolve():
        return None
    return p


def latest_verified_output(case: dict) -> dict | None:
    # 'skipped' = the operator's explicit fast-mode (no checks) output; it is
    # downloadable like a verified one. 'blocked' is never returned.
    for rec in reversed(case.get("outputs", [])):
        if rec.get("verify_status") in ("verified", "skipped"):
            return rec
    return None


def save_verify(case_id: str, name: str, report: dict) -> None:
    # verify reports quote workbook cell values in error messages (that's the
    # point — the operator must see the exact diff), so they are written like
    # the output workbook itself: ungated, inside the case dir.
    d = case_dir(case_id)
    config.atomic_write_text(d / f"{name}.verify.json",
                             json.dumps(report, ensure_ascii=False, indent=1))


def list_cases(limit: int = 20) -> list[dict]:
    root = consol_dir()
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir(), reverse=True):
        if not p.is_dir() or not _CASE_ID_RE.fullmatch(p.name):
            continue
        case = load_case(p.name)
        if case:
            out.append(case)
        if len(out) >= limit:
            break
    return out
