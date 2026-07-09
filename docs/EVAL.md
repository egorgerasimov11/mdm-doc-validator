# EVAL.md — how mdmdoc measures itself (and how to read the numbers)

Written with the 2026-07 audit wave (M1/M6/M7). The measurement problem this
solves: the real corpus is 18 PII documents, so verdict accuracy moves ±5.5pp
per document and the Wilson 95% CI spans half the unit interval — raw accuracy
on it is closer to noise than signal, and "improvements" have looked like
regressions whenever the machine got safer or the gold labels went stale.

## Metrics (per eval run)

| metric | meaning |
|---|---|
| `n` | documents ATTEMPTED = scored + crashed. Missing files are excluded but surfaced as `skipped_missing`. |
| `n_scored` | documents the pipeline completed on. Per-field metrics use this denominator. |
| `crashes`, `crash_rate` | pipeline exceptions. **Policy (M1): a crash is never a hit, and it costs like an NMR** — it fails loudly to a human and ships nothing wrong (unlike a false ACCEPT). Crash on a gold `invoice` counts as an invoice false-accept, so the adoption gate (FA must be 0) refuses candidates that crash on invoices. |
| `verdict_accuracy` (+CI) | exact verdict matches over `n`. Read it WITH the Wilson CI: on 18 docs a ±0.2 swing is inside the interval. |
| `unsafe_error_rate` | machine SOFTER than gold (dangerous direction), over `n`. The number that must stay at/near 0. |
| `safe_disagreement_rate` | machine STRICTER than gold — often a stale gold label, see gold_review / staleness. |
| `verdict_cost` | asymmetric cost: unsafe gaps ×3, safe gaps ×1. The primary tuning target — a system that gets more conservative scores BETTER here even when raw accuracy dips. |
| `invoice_false_accept_rate` | gold invoices not REJECTed (crashes included). Hard 0 requirement. |
| `precedent_relaxations{,_unconfirmed}` | labels that would RELAX a live verdict (C11 guardrail visibility). |
| `leakage_count` | strict sweep over eval/dataset/prompt artifacts. Hard 0 requirement. |

History note: metrics before the `honest-metrics-v2` step change excluded
crashed documents from every denominator — do not compare raw numbers across
that boundary without re-reading this policy.

## Two strata: real vs synthetic (never mixed)

- **real** (`mdmdoc eval` / `--dataset real`): dataset/labels.jsonl + the PII
  corpus (mini only). Writes the headline artifacts: `eval/report.md`,
  `eval/history.jsonl`, `eval/last_results.json`. This is the track record.
- **synthetic** (`--dataset synthetic`): `eval/synthetic/` — a generated,
  PII-free corpus with known ground truth (43+ docs: EN/DE/ES letters ×
  signature modes × IBAN validity, statements, payment instructions, invoices,
  letterheads, voided checks, W-9 variants incl. boxed TIN and the
  Individual+EIN conflict). Writes `eval/synthetic_*` artifacts ONLY. Run it
  offline with `--engine deterministic` (no Ollama needed) — CI-friendly.
- `--dataset both` runs the two streams sequentially; no combined metric is
  ever persisted.

Synthetic ground truth is honest by construction:
- `verdict_gold` = the REAL rules engine folded over the TRUE fields (never
  hand-coded verdicts); regenerating after a rule change diffs the labels.
- `det_expected` = what today's deterministic pipeline actually produces on
  the PDF — the CI regression lock (`tests/test_synth_corpus.py`).
- `mdmdoc synth-gen --check` proves the committed corpus matches a fresh
  regeneration (labels bytes + per-doc extracted text).
- Identities are invented; IBANs are mod-97-valid fakes; EIN `00-…` /
  SSN `000-…` come from never-assigned ranges; every label line passes the
  strict leak gate, and the fakes are registered so the sweep allows them.

The synthetic stratum answers "does the pipeline uphold its safety contract on
inputs with KNOWN truth" (unsafe_error_rate 0 offline at generation time); the
real stratum answers "how does it do on the actual paperwork". Model quality
conclusions still come from the real stream — synthetic letters are cleaner
than real scans by construction.

## Gold-label lifecycle + active learning (M7)

Labels carry `label_ts` (first labeling), `last_confirmed_ts` and
`confirm_count` — written ONLY when a human submits a review (no auto-mutation
path exists). Every recorded real eval updates `eval/gold_staleness.json`:
per-document disagreement counters since the label was last confirmed,
reset on re-confirmation. The report section **"Gold staleness — re-confirm
these first"** ranks documents by accumulated disagreements (unsafe direction
first): that ranked list is the active-learning signal — re-confirming the top
entry removes the most measurement noise per unit of operator time. It
complements `gold_review` (machine-stricter-than-gold queue, proposes label
re-review, never mutates).

Workflow when the machine beats a stale gold: the doc tops the staleness list
→ open it in the review UI → re-confirm (or correct) → counters reset, and if
the verdict was RELAXED vs the machine you must tick "confirm verdict" (C11).
