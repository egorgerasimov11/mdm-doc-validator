# External Evidence (`web_enrichment`)

An **evidence layer, never a decision layer.** It corroborates a document's
*public* identifiers against outside registries and returns advisory hints. The
rule engine + `rules/*.yaml` remain the only deciders (project invariant #1).
The run page carries a permanent banner: **"web did not decide this verdict."**

## Turning it on

Opt-in, off by default (and off in the BTP image — the sealed service makes no
outbound calls):

```bash
export MDMDOC_WEB_EVIDENCE=1          # session-wide
mdmdoc check-bank statement.pdf --web-evidence   # or per-run
```

`mdmdoc doctor` shows the current posture. When disabled, `web_enrichment.gather`
returns nothing and no network call is made.

## What it checks (MVP)

| # | check | source | tier | offline? |
|---|---|---|---|---|
| 1 | ABA routing checksum | FFIEC/Fed mod-10 algorithm | 1 | ✅ always |
| 1 | bank exists / ACTIVE, name vs document | **FDIC BankFind** (`banks.data.fdic.gov`) | 1 | needs network |
| 1 | routing → owning bank name | **Fed E-Payments Routing Directory** (connector) | 1 | needs connector |
| 1 | routing EXISTS in live directories | **3-source ladder**: usbanklocations → paymentlabs (gov/Treasury/DoD payees) → wise (merged/renamed banks) — real if ANY finds it, `not_found` only when all three provably miss | 3 | needs network |
| 2 | BIC syntax + ISO country + country-vs-document | ISO-9362 (offline) | 1 | ✅ always |
| 2 | BIC → institution name/address | SWIFTRef (licensed, optional connector) | 2 | needs connector |
| 3 | legal-entity match (holder / Line 1) | **GLEIF** (`api.gleif.org`) | 1 | needs network |
| 3 | filer match (holder / Line 1) | **SEC EDGAR** (`efts.sec.gov`) | 1 | needs network |

Each row lands as a `NOTE` finding **and** a row in the run page's *External
evidence* panel, with status `found` / `conflict` / `not_found` / `unavailable`,
a source link, the trust tier, and a UTC timestamp.

*Source trust tiers:* **1** official registry/regulator · **2** official
institution domain · **3** aggregator (not decision-grade) · **4** never emitted.

## Hard privacy rule (egress)

Only **routing/ABA numbers, SWIFT/BIC codes, bank names and company names** ever
leave the machine. Full **TIN/SSN/EIN, account numbers and IBANs are never
sent** — enforced by `web_enrichment.egress.assert_safe_outbound`, the outbound
mirror of the persistence leak gate, which every request passes through before a
socket opens (see `docs/PRIVACY.md`). Personal names are not queried either: the
entity connector only sends names that look like organisations.

## Optional connectors (env)

| env var | purpose | default |
|---|---|---|
| `MDMDOC_WEB_EVIDENCE` | master opt-in switch | unset (off) |
| `MDMDOC_WEB_TIMEOUT` | per-request timeout, seconds | `6` |
| `MDMDOC_WEB_USER_AGENT` | outbound User-Agent (SEC needs a descriptive one) | `mdmdoc/0.1 …` |
| `MDMDOC_FED_ROUTING_URL` | Fed routing-directory lookup; `{aba}` template → JSON `{"name","active"}` | unset → `unavailable` |
| `MDMDOC_SWIFT_LOOKUP_URL` | licensed SWIFT directory; `{bic}` template → JSON `{"name","address"}` | unset → skipped |

The Federal Reserve directory and SWIFTRef have no free public JSON API, so they
are connector slots: point them at an operator-hosted mirror / licensed feed to
enable the deeper cross-checks. Without them the offline checksum/syntax checks
and the free FDIC/GLEIF/SEC lookups still run.

## Guarantees (and where they live in code)

- **Never decides** — `evidence.Evidence.to_finding()` hard-codes
  `severity="NOTE", verdict_effect=None`; `gather()` re-filters as a backstop.
  The pipeline appends web findings *after* `decide()` has already run.
- **Opt-in / BTP-off** — `web_enrichment.enabled()`; `MDMDOC_WEB_EVIDENCE=0` in
  `btp/Dockerfile`.
- **Privacy** — `egress.assert_safe_outbound` on every outbound query.
- **Fail-soft offline** — `http.get_json` returns `None` on any error; the
  connector emits an `unavailable` hint.
- **Deterministic eval** — `evalrun` calls the pipeline with
  `web_evidence=False`; metrics never depend on the network.
