"""Shared helpers for the schema readers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..consensus import family_of  # noqa: F401  (re-exported)


@dataclass
class Field:
    value: str = ""
    pretty: str = ""
    status: str = "absent"             # confirmed | checksum_ok | review | absent
    page: int | None = None
    bbox_pct: list | None = None
    evidence: str = ""                 # the line the value was read from
    voices: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"value": self.value, "pretty": self.pretty or self.value, "status": self.status,
                "page": self.page, "bbox_pct": self.bbox_pct, "evidence": self.evidence,
                "voices": list(self.voices)}


def absent() -> Field:
    return Field()


_WS = re.compile(r"\s+")


def norm_text(s: str) -> str:
    """Voting key for free text: case, punctuation and spacing folded."""
    s = (s or "").upper()
    s = re.sub(r"[^0-9A-ZÀ-ɏЀ-ӿ぀-ヿ一-鿿가-힯 ]+", " ", s)
    return _WS.sub(" ", s).strip()


def lines_of(page: dict) -> dict[str, list[dict]]:
    """engine_id → lines with bbox_pct (from the extractor's pages_out entry)."""
    return page.get("lines") or {}


def find_line(page: dict, needle: str, *, digits: bool = False) -> tuple[str, list | None]:
    """First line (any engine) containing the value → (engine_id, bbox_pct)."""
    key = re.sub(r"\D", "", needle) if digits else norm_text(needle)
    if not key:
        return "", None
    for eid, lines in lines_of(page).items():
        for ln in lines:
            t = ln.get("text") or ""
            hay = re.sub(r"\D", "", t) if digits else norm_text(t)
            if key in hay and ln.get("bbox_pct"):
                return eid, ln["bbox_pct"]
    return "", None


def looks_like_label(s: str) -> bool:
    return len(s) <= 2 or bool(re.fullmatch(r"[\W_]+", s))


def anchored(readings: dict[str, str], rx: re.Pattern) -> dict[str, str]:
    """engine_id → value for the first line of each transcript matching `rx`
    ("label: value" on one line, or the value on the next non-empty line)."""
    out: dict[str, str] = {}
    for eid, text in (readings or {}).items():
        lines = [ln.strip() for ln in (text or "").split("\n")]
        for i, ln in enumerate(lines):
            m = rx.match(ln)
            if not m:
                continue
            val = (m.group("v") or "").strip(" :：-–|\t")
            if not val:
                val = next((l for l in lines[i + 1:i + 3] if l.strip()), "").strip(" :：-–|\t")
            if val and not looks_like_label(val):
                out[eid] = val
                break
    return out


def vote(cands: dict[str, str], *, key=norm_text) -> tuple[str, str, list[str]]:
    """cands: engine_id → raw value. → (winning raw value, status, voices).
    Two engine FAMILIES agreeing (after `key`) = confirmed; otherwise the
    reading of the most faithful engine is handed over for review."""
    groups: dict[str, list[str]] = {}
    for eid, raw in cands.items():
        k = key(raw)
        if k:
            groups.setdefault(k, []).append(eid)
    if not groups:
        return "", "absent", []
    order = ("textlayer", "vlm", "rapidocr", "tesseract", "applevision")

    def rank(eid: str) -> int:
        fam = family_of(eid)
        return order.index(fam) if fam in order else len(order)

    best_key, best_eids = max(groups.items(),
                              key=lambda kv: (len({family_of(e) for e in kv[1]}), -min(rank(e) for e in kv[1])))
    fams = {family_of(e) for e in best_eids}
    eid = min(best_eids, key=rank)
    raw = cands[eid]
    return raw, ("confirmed" if len(fams) >= 2 else "review"), sorted(best_eids)
