# SAP Rollout Plan — mdm-doc-validator as a corporate system

**Status:** proposal for decision · **Date:** 2026-07-09 (all metrics "as of" this date)
**Audience:** decision-makers (MDM lead, Basis/security, data owner) + the implementing team.
**How to read:** this plan cross-links the existing technical docs instead of repeating
them — each section names its source of truth. Nothing here overrides those docs.

| Companion doc | Owns |
|---|---|
| [SAP_READINESS.md](SAP_READINESS.md) | ABAP import runbook, verify-on-system checklist, MDG/Fiori flow status |
| [CORP_DEPLOY.md](CORP_DEPLOY.md) | sealed-host deployment of the analyst panel (compose, zero egress) |
| [BTP_INTEGRATION.md](BTP_INTEGRATION.md) | api-only Docker image, CF/Kyma artifacts, model topologies |
| [RULES_AUDIT.md](RULES_AUDIT.md) | per-rule decision table, tiers, open decision forks |
| [PRIVACY.md](PRIVACY.md) | masking model, leak gate, erasure, egress guard |
| [../PARITY.md](../PARITY.md) + [SYNC.md](SYNC.md) | Python ↔ ABAP parity contract and sync mechanics |
| [USER_GUIDE.md](USER_GUIDE.md) / [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | analyst/end-user view; implementation internals |

---

## 1. Executive Summary

mdm-doc-validator checks vendor banking support documents and US W-9/W-8 tax forms for
SAP MDM: it reads the document, extracts the key fields, applies an explicit,
human-approved rule set, and returns one of four verdicts
(ACCEPT / WARNING / NEED_MANUAL_REVIEW / REJECT). The AI component only reads;
**verdicts come exclusively from written rules a human has approved one by one.**

The system exists as two hand-verified twins of one logic: a **Python reference
implementation** (analyst web panel, teach/eval loop, REST API) and a **deterministic
ABAP twin** (abapGit package `ZMDMDOC`) that runs inside S/4HANA/MDG with no external
dependencies. A parity gate keeps the two honest (§3).

**Corp v1 proposal:** roll the deterministic-only ABAP validator into MDG — a BAdI that
emits masked warnings into the Change Request check log and **never blocks** — plus the
analyst web panel on a sealed corporate host with zero internet egress. No LLM is
required for v1; the optional LLM path arrives later via plain outbound HTTP to a local
Ollama host (§4, §5). Adoption is phased: PoC (done) → shadow → warn → enforce, with
explicit entry/exit criteria (§8) and a layered rollback story (§15).

Current state: parity gate green (7 guards ported, 4 n/a), 198 ABAP Unit tests
inventoried, honest eval baseline recorded (§12), rule set at a safest-only approval
posture with exactly one decision held for the rule owner (§6–7), first blind campaign
accepted by the operator, second running (§11).

## 2. Scope & Non-Goals

**In scope for corp v1:**

- Deterministic ABAP validation inside MDG (package `ZMDMDOC`, BAdI on
  `USMD_RULE_SERVICE`): PDF-with-text-layer reading, regex extraction, YAML-derived
  rules, SAP-000..008 comparison of document data vs Change Request bank data,
  masked warnings in the CR check log.
- The analyst web panel (FULL mode) on a sealed corporate host per
  [CORP_DEPLOY.md](CORP_DEPLOY.md) — local models only, zero egress, no tunnels.
- Rule governance via the approvals hard gate and the decision table in
  [RULES_AUDIT.md](RULES_AUDIT.md).

**Explicitly NOT in corp v1:**

- No LLM/vision inside SAP — the BAdI path always runs deterministic
  (`iv_llm_used = abap_false`), synchronous, no outbound HTTP.
- No web enrichment (external registries) — master-gated off (`MDMDOC_WEB_EVIDENCE=0`).
- No teach loop / model training in SAP or in the api-only image — training stays with
  the analyst panel and its adoption gate.
- No per-user accounts inside the app — identity and roles live in the corporate
  proxy/SSO layer ([CORP_DEPLOY.md](CORP_DEPLOY.md) §1).
- No BTP deployment yet — artifacts are prepared but deployment is a conscious
  later decision ([BTP_INTEGRATION.md](BTP_INTEGRATION.md)).

## 3. System Overview

Two targets of one logic, kept in lockstep:

- **Python reference** (`mdm-doc-validator`): full pipeline (OCR/vision perception →
  trainable extraction → rules → verdict), operator console, teach/eval loop, REST API.
- **ABAP twin** (`mdm-doc-validator-abap`, abapGit package `ZMDMDOC`): the deterministic
  validation pipeline only — deliberately without panel, teach loop, eval, or web
  enrichment. Self-contained, ABAP ≥ 7.50, classic regex, no external Z-dependencies.

**One version:** the exact ABAP twin a given Python checkout was verified against is
pinned as the `abap/` git submodule; a stale pin fails the parity check
([SYNC.md](SYNC.md)).

**Parity gate** (`tools/check_parity.py` + [../PARITY.md](../PARITY.md)) fails loudly on
silent drift: rule DATA (`rules/*.yaml`) must be semantically identical in both repos
(auto-synced by the generator); the 11 rule predicates must exist on both sides; every
deterministic extraction guard must carry a literal `[GUARD:x]` marker in the ABAP code
or an explicit `n/a` justification. Current status: **7 guards ported, 4 n/a**
(vision/few-shot/provenance — Python-only by design), **0 pending**.

Both sides speak the same `mdmdoc.v1` report schema, so an SAP-side check is reviewable
in the analyst console.

## 4. Architecture Options

Three prepared deployment shapes — honest comparison:

| | **A. Deterministic ZMDMDOC in MDG** (corp v1) | **B. BTP sidecar (api-only image)** | **C. Sealed-host analyst panel** (corp v1) |
|---|---|---|---|
| What runs | ABAP BAdI + interactive report inside S/4HANA | REST `POST /api/v1/check` as a pre-MDG gate (CF/Kyma) | Full operator console + teach loop via `btp/compose.full.yaml` |
| Users | Fiori end users (CR check log) | Workflow integrations | MDM analysts (rule owner incl.) |
| LLM | None, ever (deterministic path) | Required (Ollama API) | Local Ollama container, co-located |
| Egress | Zero (no outbound HTTP in BAdI path) | Depends on model topology | **Zero** — models offline-bundled, no tunnels, evidence layer pinned off |
| Blocking? | Never in v1 — warnings only | Caller decides | Advisory to the analyst |
| Coverage | PDFs with a text layer only; scans/images skipped | Full pipeline incl. vision | Full pipeline incl. vision, review, training |
| Prereqs | abapGit import + verify-on-system checklist ([SAP_READINESS.md](SAP_READINESS.md) §5) | Registry, GPU or Cloud Connector code change — **gaps documented, not promised** ([BTP_INTEGRATION.md](BTP_INTEGRATION.md) §2) | One corporate host, Docker, reverse proxy/SSO ([CORP_DEPLOY.md](CORP_DEPLOY.md)) |
| Status | Code-complete, locally verified; on-system items flagged | Artifacts ready; deployment deferred | Runbook complete; mirrors the reference production instance |

**Recommendation:** corp v1 = **A + C together.** A gives every Fiori user an automatic
typo/mismatch net with zero new infrastructure; C gives the MDM team the full-depth
check (scans, photos, W-9s, SAP screenshot comparison) and the rule-governance panel.
B remains a later option once model topology and auth are decided.

## 5. Analysis-Engine Strategy

The Python side has a first-class **engine mode** (env `MDMDOC_ENGINE` > operator panel
setting > default; see `src/mdmdoc/config.py`):

| Mode | Behavior |
|---|---|
| `auto` (default) | deterministic + fast LLM; strong LLM tier only on weakness |
| `deterministic` | OCR + patterns + rules ONLY — no LLM, no vision |
| `llm-first` | strong LLM tier from the start (quality mode) |
| `dual` | `auto` + a per-field deterministic-vs-LLM comparison artifact (side-by-side engine table; disagreements surface as a WARNING finding, agreement as a NOTE) |

**Graceful degradation is the design guarantee:** any mode that needs the LLM
auto-degrades to `deterministic` with a WARNING finding (`ENGINE-001`) when the model
host is down — **a production check never fails because of the LLM.** The rules engine
operates on deterministically extracted fields, so verdicts remain available without
any model. The ABAP BAdI path is the same guarantee taken to its limit: it is
deterministic always, by construction.

Consequence for the rollout: the LLM is an *accuracy upgrade*, not a dependency. Corp
v1 ships with no LLM in SAP; the sealed-host panel uses local models; a later in-SAP
LLM option needs only plain outbound HTTP from the application server to a local Ollama
host — no SM59 destination, no SICF service ([SAP_READINESS.md](SAP_READINESS.md) §8),
and the same degrade-don't-break behavior (finding `LLM-001`, run continues).

## 6. Rule Governance

Rules are YAML (`rules/banking.yaml`, `rules/w9.yaml` — single source of truth in the
Python repo), and **no rule fires without a human approval**:

- **Approvals hard gate** (`enforce_approvals` default ON, env `MDMDOC_RULE_GATE=1`):
  a rule fires only if approved in the panel (`/ui/rules/approve`) AND its content hash
  still matches — **any edit auto-reverts the rule to pending**. Rejected rules are
  skipped. A *pending* applicable rule holds the document at **NEED_MANUAL_REVIEW**
  (finding `RULE-GATE`) — nothing is ever silently accepted
  ([DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) §4.2).
- **Tiers of trust** (provenance metadata, per [RULES_AUDIT.md](RULES_AUDIT.md) and the
  PARITY.md coordination requests): `tier: corp` (defensible by policy — the only tier
  that ships in corp v1), `experimental` (new subtype being proven), `learned`
  (operator heuristic — does NOT ship in corp v1).
- **Safest-only posture applied 2026-07-09:** banking **14 approved / 1 pending** —
  the pending one is BNK-002 (email hard-REJECT), deliberately held for the rule
  owner's decision (fork #1 in [RULES_AUDIT.md](RULES_AUDIT.md)); W-9 **10 approved**.
  W-9 approvals are safe by construction: NMR/WARNING-only effects cannot produce a
  wrong verdict — only hard-REJECT rules can (§7).
- **Promotion path:** propose flow (dispute button / free-text) → readable YAML diff →
  rule-owner Approve in the panel → "Regenerate for SAP" pushes the YAML into the ABAP
  repo → abapGit pull → activate → transport. New program logic (a new predicate) is
  explicitly escalated to the developer and hand-ported under the parity gate.
- **Kill switch:** the approvals gate doubles as the emergency brake — un-approving or
  rejecting a rule in the panel stops it firing on the next check, with no deployment
  (§15).

## 7. "Wrong Rules" Risk & Mitigation

**Only hard-REJECT rules can hurt.** Findings whose effect is NMR/WARNING/NOTE fail
safe: the worst outcome is an unnecessary human look. A wrong verdict — auto-rejecting
a legitimate document — is only possible from the three hard-REJECT rules (invoice,
email, editable file offered as bank proof).

**Case study — BNK-002 (email as bank confirmation).** The rule as written REJECTs any
email. The MDM checker skill, however, allows a vendor-email exception for HCP vendors —
context the validator cannot see. An unconditional REJECT would therefore falsely kill
legitimate HCP cases (the blind corpus contains exactly four such emails). Resolution
options (soften to NEED_MANUAL_REVIEW, keep REJECT, or change the skill) are laid out
as **decision fork #1 in [RULES_AUDIT.md](RULES_AUDIT.md)**; the rule is held *pending*
— which by gate semantics means matching documents go to manual review, never
auto-accept and never false auto-reject — until the rule owner decides.

**The process this generalizes to:** decision table with recommendation and tier
([RULES_AUDIT.md](RULES_AUDIT.md)) → explicit rule-owner Approve/Reject/Correct in the
panel → the gate enforces the decision (hash-bound, edit reverts to pending). No rule
reaches production — Python or ABAP — any other way.

## 8. Phased Adoption

| Phase | What runs | Entry criteria | Exit criteria |
|---|---|---|---|
| **0. PoC — DONE** | Python reference in team production; ABAP twin code-complete; parity gate green; baseline eval tagged; blind campaign #1 accepted, #2 in flight | — | Done as of 2026-07-09 |
| **1. Import & verify-on-system** | `ZMDMDOC` imported to DEV via abapGit; no BAdI yet | Basis approval, transport route | All 198 ABAP Unit tests green; `ZMDMDOC_SETUP` reports **GO**; every ⚠ item in [SAP_READINESS.md](SAP_READINESS.md) §5 closed (top risk: `CL_ABAP_GZIP` zlib inflation on a real PDF); e2e test: CR + wrong-IBAN attachment → SAP-001 warning in the check log |
| **2. Pilot — SHADOW** | BAdI active for a pilot scope (BP model, limited CR types); warnings land in the check log but are reviewed by the MDM team only; **never blocks** | Phase 1 exit; rule posture confirmed by rule owner (incl. fork #1) | Agreed CR volume processed (e.g. 4–6 weeks); shadow agreement with analyst decisions ≥ target (§12); false-positive rate acceptable to rule owner; no performance complaints (BAdI adds milliseconds, anchored to bank-details entity + PDF attachments only) |
| **3. WARN** | Same, but requestors are expected to act on warnings (guide: [USER_GUIDE.md](USER_GUIDE.md) Part 2) | Phase 2 exit; comms to requestors | Manual-review share within target; no unresolved wrong-rule incidents over the observation window |
| **4. ENFORCE (optional, later)** | E-type (blocking) messages for hard-REJECT findings only | Per-rule rule-owner sign-off; false-ACCEPT and false-REJECT evidence from phases 2–3; rollback rehearsed | Steady state; periodic eval cadence (§11) |

The BAdI's message mapping already supports both stances: type `W` (warning) for
everything except findings whose effect is REJECT, which map to type `E` — in corp v1
(phases 2–3) no approved hard-REJECT rule is active in the in-SAP comparison path, so
the system never blocks; phase 4 is a deliberate, reversible decision.

## 9. MDG/Fiori Integration

One paragraph, because [SAP_READINESS.md](SAP_READINESS.md) owns the detail: an end
user attaches a document to a Change Request and presses **Check**; the BAdI
(`IF_EX_USMD_RULE_SERVICE~CHECK_ENTITY`, anchored to the bank-details entity so it
fires once per check) reads the CR's bank data and GOS attachments, deterministically
extracts the PDF (text layer only — scans are skipped in this path), compares
field-by-field, and emits masked findings `SAP-000..008` into the CR check log, e.g.
`[SAP-001] IBAN mismatch DE**…4931 vs DE**…4999` with the first differing position.
The field mapping is discovered live (`ZMDMDOC_MDG_DISCOVER`) and persisted in the
optional customizing table `ZMDMDOC_MAP`; setup, doctor, and go-live checks are one
report, `ZMDMDOC_SETUP`, with a GO/NO-GO summary. A persistent note to the Data Owner
(`add_cr_note`) is specified but is on-system work
([SAP_READINESS.md](SAP_READINESS.md) §7).

## 10. Security & Compliance

Owned by [PRIVACY.md](PRIVACY.md); the load-bearing invariants:

- **TIN/SSN/EIN is masked in every SAP-facing deployment** — the ABAP twin and the
  api-only image have no operator console and mask tax IDs unconditionally. Only the
  local Python console can reveal them (`MDMDOC_TIN_VALUES`, default full there), and
  even there the reveal never reaches training data, outbound calls or `reasoning.md`.
- **Every persisted byte passes a leak gate** (`assert_no_leak`): a leaking write
  *raises* instead of leaking; the eval sweep hard-fails on `leakage_count > 0`
  (baseline: 0). The one observed false positive — a phantom-TIN kind-conflict —
  was fixed on 2026-07-09 with a kind-conflict guard; the gate itself was not relaxed.
- **Zero egress on the sealed host** ([CORP_DEPLOY.md](CORP_DEPLOY.md) §8): web
  evidence pinned off, model traffic stays on the private compose network,
  `trust_env=False` (no silent proxy routing), no SSH tunnels, models arrive as an
  offline bundle. The only egress-capable code path is opt-in, NOTE-only, and guarded
  by an outbound choke point that blocks account numbers, IBANs, and TINs by pattern.
- **In SAP:** the BAdI path makes no RFC/HTTP calls at all; check-log messages carry
  masked values only; authorizations are a short, explicit list
  ([SAP_READINESS.md](SAP_READINESS.md) §3 — no S_ICF unless the optional LLM is used).
- **Erasure unit** for one document is defined and small: `inbox/<sha16>__*` +
  `runs/<sha16>/` (+ one labels line if labeled), including backups.

## 11. Operations & Monitoring

- **Shadow statistics (phases 2–3):** verdict distribution, per-rule hit counts
  (`findings.json` names every fired rule id), SAP-00x mismatch rates, share of CRs
  with at least one warning, and shadow agreement vs analyst decisions. Sources: the
  CR check log on the SAP side; `runs/` artifacts and the console on the panel side.
- **Eval cadence:** re-run `mdmdoc eval` with a dated tag before every phase gate and
  after every rules or model change; the eval history feeds the adoption gate and
  enforces zero leakage. Baseline tag: `baseline-sap-ready-20260709`.
- **Corpus growth:** blind campaigns extend the labeled corpus — campaign #1 (6 real
  vendor documents, verdicts accepted by the operator) is folded in; campaign #2
  (7 unique fresh documents, incl. US/JP/CO/LV forms) is running now. Every operator
  correction becomes a masked labeled example.
- **Model lifecycle (panel only):** candidate models never self-promote — automatic
  quality gate, then explicit Adopt with one-click Rollback on the Training page.
- **Housekeeping:** backup matrix and update path (image rebuild on a connected host,
  never overwriting `approvals.json` or data volumes) are in
  [CORP_DEPLOY.md](CORP_DEPLOY.md) §6–7; rule YAML changes require a re-approval pass
  by design.

## 12. KPIs & Acceptance

**Honest baseline** — mini host, 18 labeled real documents, eval tag
`baseline-sap-ready-20260709`, all figures **as of 2026-07-09** (a final re-run is in
flight; update this table from it before the Phase 2 gate):

| Metric | Baseline | Proposed target (phase 3 gate) |
|---|---|---|
| doc_type accuracy | 0.882 | ≥ 0.90 |
| verdict accuracy | 0.706 | ≥ 0.85 |
| IBAN / SWIFT field accuracy | 1.0 | 1.0 (hold) |
| account_holder / account_number | 0.636 | ≥ 0.80 |
| W-9 fields | ~1.0 | hold |
| W-9 TIN | 0.833 | ≥ 0.90 |
| JSON-valid outputs | 1.0 | 1.0 (hold) |
| Leakage count | 0 | **0 — hard requirement, always** |

**Operational acceptance criteria** (targets to be confirmed by the rule owner at the
Phase 2 kickoff):

- **False-ACCEPT ceiling:** zero tolerated on documents matching a hard-REJECT class
  (invoice / editable file as bank proof) across the shadow window.
- **Shadow agreement:** validator verdict agrees with the analyst's final decision on
  ≥ 90 % of pilot CRs (disagreements are individually reviewed, not averaged away).
- **Manual-review share:** NEED_MANUAL_REVIEW on ≤ ~30 % of documents once the rule
  posture is settled (pending rules inflate this by design — that is the gate working).
- **Performance:** no measurable CR-check latency complaint (BAdI budget: milliseconds).

## 13. Ownership

| Role | Who | Owns |
|---|---|---|
| **Rule owner** | The MDM analyst (business owner of the rule set) | Every Approve/Reject/Correct in the panel; decision forks in [RULES_AUDIT.md](RULES_AUDIT.md); phase-gate sign-off on KPIs |
| **Developer (Python reference)** | Current maintainer | Pipeline, panel, eval/teach loop, parity gate, rule generator |
| **ABAP maintainer** | Assigned ABAP developer | abapGit imports, verify-on-system items, BAdI lifecycle, transports, on-system `add_cr_note` work |
| **Basis / security** | Corporate Basis + security officer | Import approval, authorizations, reverse proxy/SSO and host sealing, sign-off on [CORP_DEPLOY.md](CORP_DEPLOY.md) §1 threat model |

Cross-repo handoffs go through the coordination section of
[../PARITY.md](../PARITY.md), never through editing the other side's files.

## 14. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Wrong hard-REJECT rule auto-rejects a legitimate document | Low (1 of 3 hard rules disputed) | High | Safest-only posture; BNK-002 held pending (→ manual review, §7); shadow phase before any requestor-visible effect; approvals kill switch |
| Python ↔ ABAP parity drift | Medium over time | Medium | `check_parity.py` gate (non-zero exit on drift), `[GUARD:x]` receipts, one-version submodule pin, "pending ports" list must be empty |
| Bus factor (single developer knows both twins) | Medium | Medium | Complete doc set (this table's companions); generated rule data; parity manifest as the contract; ABAP twin is self-contained and unit-tested |
| SAP release/upgrade changes MDG API surface | Medium | Medium | All release-dependent seams are flagged verify-on-system with adaptation notes ([SAP_READINESS.md](SAP_READINESS.md) §5); re-run `ZMDMDOC_SETUP` pre-flight after every upgrade |
| Kernel PDF inflation (`CL_ABAP_GZIP` zlib) fails on target | Unknown until tested | Medium | Documented top risk; 3 inflation strategies + print-to-PDF workaround; tested in Phase 1 before anything is enabled |
| Model drift / regression (LLM phases, panel) | Low | Low–Medium | Deterministic floor (verdicts never depend on the LLM); adoption gate: candidate-only training, eval before Adopt, one-click Rollback |
| Pending-rule NMR volume overwhelms analysts | Low | Low | Approval cadence after every rules update (edits revert to pending by design); manual-review share KPI |
| Model host outage | Expected occasionally | Low | `ENGINE-001` graceful degradation — checks complete deterministically (§5); SAP path unaffected (no LLM at all) |

## 15. Rollback

Layered, cheapest first:

1. **Phase fallback:** enforce → warn → shadow is a messaging/expectation change, not a
   technical one — the BAdI mapping already distinguishes W and E messages.
2. **Approvals kill switch (no deployment):** un-approve or reject a misbehaving rule
   in `/ui/rules/approve` — it stops firing on the next check; matching documents fall
   back to NEED_MANUAL_REVIEW rather than a wrong verdict. On the SAP side, rule
   changes normally travel via regenerate → abapGit → transport; for emergencies the
   report's `p_rules` JSON override changes behavior without a transport
   ([SAP_READINESS.md](SAP_READINESS.md) §8).
3. **BAdI deactivation:** SE19 → deactivate the `USMD_RULE_SERVICE` implementation —
   MDG returns to stock behavior immediately; the imported package can stay dormant.
4. **Panel host:** recreate the previous container image; all state (runs, labels,
   `approvals.json`) lives in volumes/bind mounts and survives
   ([CORP_DEPLOY.md](CORP_DEPLOY.md) §6–7).
5. **Model rollback (panel):** the adoption gate keeps the previous Modelfile —
   one-click Rollback on the Training page.

---

## Appendix

**Rule inventory:** the authoritative per-rule table (what each rule does, verdict
effect, live hit counts, recommendation, `tier`/`source`) is
[RULES_AUDIT.md](RULES_AUDIT.md). Approval state is runtime data in
`rules/approvals.json` (never deployed over, never copied to ABAP).

**Glossary**

- **Verdict** — overall result of a check: ACCEPT / WARNING / NEED_MANUAL_REVIEW /
  REJECT (precedence: REJECT > NMR > WARNING > ACCEPT).
- **Finding** — one observation tied to a named rule (`BNK-…` banking, `W9-…` tax,
  `SAP-…` CR comparison, `ENGINE-…` engine status, `RULE-GATE` pending-approval hold).
- **Hard-REJECT rule** — a rule whose effect is REJECT; the only rule kind that can
  produce a wrong verdict (§7).
- **Approvals gate** — the hard gate that lets only human-approved, hash-matching
  rules fire; pending rules force manual review.
- **Parity gate** — the automated check that the Python and ABAP twins carry the same
  rules, predicates, and guards ([../PARITY.md](../PARITY.md)).
- **Shadow mode** — validator runs and logs in MDG, output reviewed by the MDM team
  only; no requestor-facing expectations, never blocks.
- **Masked value** — a value with most characters hidden (e.g. `DE**…4931`); tax IDs
  are masked everywhere, under every policy.
- **`mdmdoc.v1`** — the shared report schema both twins emit.
