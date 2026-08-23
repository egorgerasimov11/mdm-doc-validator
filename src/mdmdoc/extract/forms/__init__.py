"""Schema readers over the offline extractor's output.

`extract_document` returns identifier tokens with document labels; these readers
turn them into a fixed schema a host application can compare against its own
record — a W-9 (name / classification / TIN / address) or a bank document
(holder / bank / country / IBAN / account / routing / SWIFT). Deterministic, no
model: every value carries the status the consensus layer gave it, or `review`
when only one engine read it.
"""
from __future__ import annotations

from .common import Field, absent, family_of, find_line, lines_of, norm_text, vote  # noqa: F401
