#!/usr/bin/env python3
"""
check_parity.py — drift detector between the Python validator and its ABAP twin.

The two validators are two TARGETS of one logic (Python = local web + teach/eval;
ABAP = deterministic validator inside S/4HANA/MDG). They cannot auto-sync — one is
hand-written Python, the other hand-written ABAP — but they MUST NOT drift silently.
This checker fails loudly the moment they diverge, so a divergence is a caught error
instead of a wrong verdict in production (as happened with the US-numeric-IBAN case).

What it checks (read-only against the ABAP repo — never edits it):
  1. RULE DATA parity — rules/banking.yaml + w9.yaml are semantically identical in
     both repos (the source of truth is Python; `/rules/regenerate` copies them over).
  2. PREDICATE-SURFACE parity — the set of `when.check` predicates Python implements
     (predicates.REGISTRY) equals the set the ABAP rule engine dispatches.
  3. YAML COVERAGE — every `when.check` used by a rule is implemented on BOTH sides.
  4. MANIFEST freshness — PARITY.md lists exactly the current predicates (a new
     predicate forces a conscious manifest entry), and its "Pending ABAP logic ports"
     section is empty (a listed pending port = known, tracked drift → non-zero exit).
  5. GUARD parity — every deterministic stage_b guard (module-level `def _x(ext, raw)`)
     has a PARITY.md "## Guards" entry with a conscious status: `ported` requires a
     `[GUARD:x]` marker in zcl_mdmdoc_extract, `n/a` documents a Python-only guard
     (vision/few-shot/provenance), `pending` = tracked drift → non-zero exit. A new
     guard without a manifest entry, or an ABAP marker without a Python guard, fails.
  6. ONE VERSION — the abap/ submodule pin equals the live ABAP checkout's HEAD
     (docs/SYNC.md). A stale pin = the Python repo ships an outdated ABAP twin.

Exit 0 = in sync (or ABAP repo absent → skipped). Exit 1 = drift. Run it manually,
in a git pre-commit hook, or in CI:  `python3 tools/check_parity.py`
Point at a non-default ABAP checkout with MDMDOC_ABAP_HOME.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

PY_ROOT = Path(__file__).resolve().parents[1]


def _resolve_abap_root() -> Path:
    """MDMDOC_ABAP_HOME > live sibling checkout > the abap/ submodule.
    The sibling wins over the submodule on dev machines: it is the LIVE working
    copy; the submodule is the pinned 'one version' snapshot (freshness check
    below keeps the two honest)."""
    env = os.environ.get("MDMDOC_ABAP_HOME")
    if env:
        return Path(env)
    sibling = Path.home() / "Projects" / "mdm-doc-validator-abap"
    if sibling.exists():
        return sibling
    sub = PY_ROOT / "abap"
    if (sub / "src").exists():
        return sub
    return sibling


ABAP_ROOT = _resolve_abap_root()
DOC_CLASSES = ("banking", "w9")

# ABAP `when:` OPERATORS (not `check:` predicates) — the rule engine handles these
# inline; everything else lowercase in a WHEN-label is a check-predicate name.
_ABAP_WHEN_OPERATORS = {"always", "field_missing", "flag_true", "flag_false",
                        "equals", "in", "regex_mismatch", "check"}


# --- source-of-truth extractors (all pure, unit-testable) ---------------------
def python_predicates(py_root: Path = PY_ROOT) -> set[str]:
    """The predicate names Python implements, parsed from predicates.REGISTRY
    (parsed, not imported, so the checker runs without installing the package)."""
    text = (py_root / "src" / "mdmdoc" / "rules" / "predicates.py").read_text()
    m = re.search(r"REGISTRY\s*=\s*\{(.*?)\}", text, re.S)
    if not m:
        raise RuntimeError("could not find REGISTRY in predicates.py")
    return set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))


def abap_check_names(abap_root: Path = ABAP_ROOT) -> set[str]:
    r"""The predicate names the ABAP rule engine dispatches, parsed from the
    WHEN-labels of zcl_mdmdoc_rules.clas.abap (``WHEN `name`.``) minus the
    when-operators."""
    text = (abap_root / "src" / "zcl_mdmdoc_rules.clas.abap").read_text()
    labels = set(re.findall(r"WHEN\s+`([a-z_]+)`", text))
    return labels - _ABAP_WHEN_OPERATORS


def yaml_rules(path: Path) -> dict:
    """{rule_id: rule_dict} for semantic comparison (ignores formatting)."""
    data = yaml.safe_load(path.read_text()) or {}
    out = {}
    for r in data.get("rules", []):
        if isinstance(r, dict) and r.get("id"):
            out[r["id"]] = r
    # the iban_length table is behaviour too — fold it in under a sentinel key
    if data.get("tables"):
        out["__tables__"] = data["tables"]
    return out


def yaml_checks_used(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text()) or {}
    return {r["when"]["check"] for r in data.get("rules", [])
            if isinstance(r.get("when"), dict) and "check" in r["when"]}


def python_guards(py_root: Path = PY_ROOT) -> set[str]:
    """Deterministic stage_b guard functions: module-level `def _name(ext)` /
    `def _name(ext, raw)`. Helpers with other signatures (e.g. _cross_note) are
    excluded by the signature shape itself."""
    text = (py_root / "src" / "mdmdoc" / "stage_b.py").read_text()
    return set(re.findall(
        r"^def _([a-z0-9_]+)\(ext: Extraction(?:, raw: RawDoc)?\) -> None:",
        text, re.M))


def abap_guard_markers(abap_root: Path = ABAP_ROOT) -> set[str]:
    """`[GUARD:name]` markers in the ABAP extract class — the hand-port receipts."""
    text = (abap_root / "src" / "zcl_mdmdoc_extract.clas.abap").read_text()
    return set(re.findall(r"\[GUARD:([a-z0-9_]+)\]", text))


def submodule_staleness(py_root: Path = PY_ROOT,
                        abap_root: Path = ABAP_ROOT) -> str | None:
    """The abap/ submodule pin must equal the live ABAP checkout's HEAD —
    that pin IS the 'one version' guarantee (a Python-repo checkout carries
    the exact ABAP twin it was verified against). Offline check: compares the
    two local SHAs; skipped when either side is absent or they are the same
    working tree."""
    import subprocess
    sub = py_root / "abap"
    if not (sub / ".git").exists() or not (abap_root / ".git").exists():
        return None
    if sub.resolve() == abap_root.resolve():
        return None

    def head(cwd: Path) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                              capture_output=True, text=True).stdout.strip()

    pin, live = head(sub), head(abap_root)
    if pin and live and pin != live:
        return (f"abap/ submodule pin {pin[:9]} != live ABAP HEAD {live[:9]} — "
                "bump it: (cd abap && git pull origin main) && git add abap && commit")
    return None


def parity_manifest(py_root: Path = PY_ROOT) -> tuple[set[str], list[str], dict[str, str]]:
    """(listed predicates, pending-ABAP-port lines, guard->status) from PARITY.md."""
    p = py_root / "PARITY.md"
    if not p.exists():
        return set(), [], {}
    text = p.read_text()
    listed: set[str] = set()
    pending: list[str] = []
    guards: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        h = line.strip().lower()
        if h.startswith("## predicates"):
            section = "pred"
        elif h.startswith("## pending abap"):
            section = "pending"
        elif h.startswith("## guards"):
            section = "guards"
        elif h.startswith("## "):
            section = None
        elif section == "pred" and line.strip().startswith("- "):
            listed.add(line.strip()[2:].split()[0])
        elif section == "pending" and line.strip().startswith("- "):
            pending.append(line.strip()[2:])
        elif section == "guards" and line.strip().startswith("- "):
            body = line.strip()[2:]
            name = body.split()[0]
            m = re.search(r"—\s*(ported|n/a|pending)", body)
            guards[name] = m.group(1) if m else "?"
    return listed, pending, guards


# --- the check --------------------------------------------------------------
def run() -> int:
    problems: list[str] = []
    notes: list[str] = []

    if not ABAP_ROOT.exists():
        print(f"↷ ABAP repo not found at {ABAP_ROOT} — parity check skipped "
              "(set MDMDOC_ABAP_HOME to enable).")
        return 0

    # 1. rule DATA parity (semantic)
    for cls in DOC_CLASSES:
        py_f, abap_f = PY_ROOT / "rules" / f"{cls}.yaml", ABAP_ROOT / "rules" / f"{cls}.yaml"
        if not abap_f.exists():
            problems.append(f"data: {abap_f} is missing in the ABAP repo")
            continue
        py_r, abap_r = yaml_rules(py_f), yaml_rules(abap_f)
        only_py = sorted(set(py_r) - set(abap_r))
        only_abap = sorted(set(abap_r) - set(py_r))
        changed = sorted(k for k in set(py_r) & set(abap_r) if py_r[k] != abap_r[k])
        if only_py:
            problems.append(f"data[{cls}]: rules in Python but not ABAP: {only_py}")
        if only_abap:
            problems.append(f"data[{cls}]: rules in ABAP but not Python: {only_abap}")
        if changed:
            problems.append(f"data[{cls}]: rule bodies differ (regenerate needed): {changed}")

    # 2. predicate-surface parity
    py_p = python_predicates()
    abap_p = abap_check_names()
    if py_p - abap_p:
        problems.append(f"predicates in Python but not dispatched by ABAP: {sorted(py_p - abap_p)}")
    if abap_p - py_p:
        problems.append(f"predicates dispatched by ABAP but absent in Python: {sorted(abap_p - py_p)}")

    # 3. YAML coverage — every used check exists on both sides
    used = set().union(*(yaml_checks_used(PY_ROOT / "rules" / f"{c}.yaml") for c in DOC_CLASSES))
    for miss, side in ((used - py_p, "Python"), (used - abap_p, "ABAP")):
        if miss:
            problems.append(f"coverage: rules use check(s) {sorted(miss)} not implemented in {side}")

    # 4. manifest freshness + pending logic ports
    listed, pending, guard_manifest = parity_manifest()
    if not listed and not (PY_ROOT / "PARITY.md").exists():
        notes.append("PARITY.md absent — create it to track predicate logic parity")
    else:
        if listed != py_p:
            problems.append(f"PARITY.md predicate list is stale vs REGISTRY: "
                            f"add {sorted(py_p - listed)}, remove {sorted(listed - py_p)}")
        for line in pending:
            if line.strip().startswith("(none"):
                continue
            problems.append(f"pending ABAP logic port (tracked drift): {line}")

    # 5. guard parity — stage_b guards vs PARITY.md statuses vs ABAP markers
    py_g = python_guards()
    abap_g = abap_guard_markers()
    for g in sorted(py_g - set(guard_manifest)):
        problems.append(f"guards: stage_b guard '_{g}' has no PARITY.md entry — "
                        "decide ported / n/a / pending consciously")
    for g in sorted(set(guard_manifest) - py_g):
        problems.append(f"guards: PARITY.md lists '{g}' but stage_b has no such guard "
                        "(stale manifest entry)")
    for g, status in sorted(guard_manifest.items()):
        if status == "ported" and g not in abap_g:
            problems.append(f"guards: '{g}' is marked ported but zcl_mdmdoc_extract "
                            f"has no [GUARD:{g}] marker")
        elif status == "pending":
            problems.append(f"guards: pending ABAP guard port (tracked drift): {g}")
        elif status == "?":
            problems.append(f"guards: '{g}' has an unparseable status in PARITY.md")
    for g in sorted(abap_g - py_g):
        problems.append(f"guards: ABAP carries [GUARD:{g}] but Python has no such "
                        "stage_b guard — renamed or removed? update both sides")

    # 6. 'one version' — the abap/ submodule pin tracks the live ABAP HEAD
    stale = submodule_staleness()
    if stale:
        problems.append(stale)

    # report
    if notes:
        for n in notes:
            print(f"• note: {n}")
    if problems:
        print(f"\n✗ Python↔ABAP parity: {len(problems)} issue(s)\n")
        for p in problems:
            print(f"  - {p}")
        print("\nRule DATA drift → run the Python UI 'regenerate' or gen_rules_abap.py.")
        print("Predicate/logic drift → hand-port to ABAP (gen_rules_abap.py carries "
              "DATA only, never predicate logic) and update PARITY.md.")
        return 1
    ported = sum(1 for s in guard_manifest.values() if s == "ported")
    print(f"✓ Python↔ABAP in sync — {len(py_p)} predicates, "
          f"{sum(len(yaml_rules(PY_ROOT / 'rules' / f'{c}.yaml')) for c in DOC_CLASSES)} rule entries, "
          f"{ported}/{len(guard_manifest)} guards ported (rest n/a by design), "
          "rule data identical, no pending logic ports.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
