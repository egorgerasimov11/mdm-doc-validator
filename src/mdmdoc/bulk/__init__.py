"""bulk — MASS validation of SAP master-data tables (V-wave).

A separate product surface from the document pipeline: the input is a TABLE
(a canonical template or a raw SE16N export), the output is a per-row bucket
verdict — VALID / SUSPICIOUS / INVALID / SKIPPED — plus a summary, never the
document ACCEPT/REJECT fold. Three cases ship: banking (BUT0BK-shaped rows),
tax numbers (DFKKBPTAXNUM-shaped rows) and postal/region (address fields
against attached T005S/T005U references).

Checks are DATA-driven (rules/bulk_*.yaml) but deliberately live OUTSIDE the
document-rules approval gate: a bulk row bucket is an audit fact with a cited
rationale (ABA 3-7-1, Fed prefix ranges, ISO 13616, per-country tax formats,
T005S membership), not a processing verdict. Python-console-only — no ABAP
pair (see PARITY.md).

Privacy: the full-value result workbook is written to inbox/ (gitignored,
operator's own data); everything under runs/ goes through the leak gate with
tax numbers masked under every policy.
"""
from .engine import run_bulk  # noqa: F401
from .model import BUCKETS, BulkResult, RowVerdict  # noqa: F401
