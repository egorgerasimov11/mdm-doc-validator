"""F6: the golden deterministic-parity corpus all passes on the Python side.
The SAME cases are generated into an ABAP data class (tools/golden/gen_abap_
golden.py) so the twin must reproduce identical fields/notes — check_parity §7
verifies the ABAP copy is up to date with the corpus hash."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "golden"))

import run_golden  # noqa: E402


def test_golden_corpus_all_pass():
    results = [run_golden.run_case(c) for c in run_golden.load_cases()]
    bad = [r for r in results if not r["ok"]]
    assert not bad, "golden cases failed: " + "; ".join(
        f"{r['id']}: {r['fails']}" for r in bad)


def test_corpus_hash_stable():
    # deterministic 16-hex — feeds the ABAP generated-data freshness check
    h = run_golden.corpus_hash()
    assert len(h) == 16 and h == run_golden.corpus_hash()
