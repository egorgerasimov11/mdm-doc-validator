# CORP_DEPLOY — self-contained corporate deployment runbook

Deploy the mdmdoc validator **with the operator console** (FULL mode) plus a
co-located Ollama on a single corporate host with **no internet egress**.
Artifacts this runbook drives:

| Artifact | Purpose |
|---|---|
| `btp/compose.full.yaml` | validator (FULL mode) + ollama on one private network |
| `scripts/bundle-models.sh` | offline model provisioning (export on a connected host, import on the sealed host) |
| `btp/Dockerfile` | the app image both deployments build from |

Reference deployment (same shape, non-corp): single instance on a Mac mini,
LaunchAgent `com.victor.mdmdoc`, port 8766, `tailscale serve`, UI
`https://omen.tail461272.ts.net:8766/ui`, `MDMDOC_API_TOKEN` +
`MDMDOC_ALLOWED_HOSTS` in the environment, rules gate ON with
`rules/approvals.json` and the approval panel at `/ui/rules/approve`.

---

## 1. Threat model / trust boundary

```
 corp user ──► corp reverse proxy (TLS + SSO/IdP, role check) ──► 127.0.0.1:8766 ──► validator container ──► ollama container
                        │                                                                      (private compose network only)
                        └── only members of the MDM operator role reach the app at all;
                            a narrower "rule approver" role gates /ui/rules/*
```

**What the corporation provides (out of scope for this app):**

* A reverse proxy / SSO terminator (corp standard — verify on system) in front
  of the published port. It authenticates users against the corporate IdP and
  authorizes two roles by URL path:
  * **operator** — everything (`/ui`, `/api/v1/*`),
  * **rule approver** (the "special role") — must be the only role allowed to
    reach `/ui/rules` and `/ui/rules/approve` plus
    `POST /api/v1/rules/{doc_class}/approve`.
* TLS. The app itself speaks plain HTTP on the published loopback port.

**What the app provides (defense in depth, second factor):**

* `MDMDOC_API_TOKEN` — a single static bearer token required on every
  `/api/v1` route (constant-time `hmac.compare_digest`). Accepted as
  `Authorization: Bearer <token>` header, the `mdmdoc_token` cookie (set
  httponly/samesite=strict by the UI middleware on `/ui` page loads), or a
  `?token=` query param as last resort. Even a user who gets past the proxy
  cannot use the API without it.
* Host-header allowlist (FULL mode only): any request whose `Host` is not
  `127.0.0.1`/`localhost` or listed in `MDMDOC_ALLOWED_HOSTS` is rejected
  with `403 forbidden_host` (DNS-rebinding hardening). Set it to the exact
  hostname the proxy forwards.
* `GET /health` is deliberately unauthenticated (liveness only; it never
  probes the model host) — safe to expose to the proxy's health checker.

**Explicitly NOT built:** per-user accounts, sessions, roles, or audit-by-user
inside the app. There is exactly one shared token; user management and
role separation live entirely in the corporate proxy/SSO layer. The rule
gate's `by` field in `rules/approvals.json` is informational, not an identity
claim.

**Data sensitivity inside the boundary:** `inbox/` holds raw uploaded
documents (the most sensitive store); `runs/` holds masked artifacts. TIN/
SSN/EIN is masked in every persisted byte under every policy (privacy
invariant — `privacy.assert_no_leak` raises on a leaking write). Erasure unit
for one document: delete `inbox/<sha16>__*` + `runs/<sha16>/` (+ its
`labels.jsonl` line if labeled).

---

## 2. What must cross the air gap

Build and pull happen on a **connected staging host**; only finished
artifacts cross:

1. **The repo** — git clone/pull on the connected host, transfer by the corp
   code-sync mechanism (verify on system). Exclude nothing; the repo carries
   rules/prompts/templates the image needs. Never overwrite the target's
   `rules/approvals.json` (live human decisions — keep it out of any deploy
   rsync).
2. **The app image** (if the sealed host cannot build): on the connected host
   `docker build -f btp/Dockerfile -t mdmdoc:latest .` (build pulls from
   PyPI/apt/ghcr — that is why it cannot run inside), then `docker save`,
   transfer, `docker load`. Same for `ollama/ollama:latest`.
3. **The model bundle** — `scripts/bundle-models.sh export` (next section).
4. **`.env`** is generated ON the sealed host (the token never travels).

---

## 3. Offline model provisioning (`scripts/bundle-models.sh`)

Models used per role (`src/mdmdoc/model_client.py`):

| Role | Model | Notes |
|---|---|---|
| VISION | `qwen2.5vl:7b` | Stage A transcription of scans/images |
| TEXT | `mdmdoc-extract` (custom) | built on target `FROM qwen3:4b`; falls back to stock `qwen3:4b` until built |
| TEXT_STRONG | `qwen3:14b` | escalation tier (`--quality` / critical gaps) |
| EMBED | `nomic-embed-text` | few-shot diversity selection |

**On the internet-connected host** (needs the `ollama` CLI + running server):

```sh
scripts/bundle-models.sh export mdmdoc-models-bundle.tar
```

Pulls `qwen2.5vl:7b qwen3:4b qwen3:14b nomic-embed-text`, tars the Ollama
store, writes `mdmdoc-models-bundle.tar.sha256`. Transfer **both** files.

**On the sealed host** (compose deployment):

> Prerequisite: `btp/.env` with `MDMDOC_API_TOKEN` must already exist (§4
> step 1) — the compose file uses `${MDMDOC_API_TOKEN:?}` and every
> `docker compose` command below fails interpolation without it.

```sh
scripts/bundle-models.sh import mdmdoc-models-bundle.tar
```

This verifies the checksum, untars the store into the `mdmdoc-ollama` named
volume, starts the stack, verifies all four base models with `ollama list`,
and builds the custom model:

* `docker compose exec validator mdmdoc train --modelfile` — writes
  `models/Modelfile.mdmdoc-extract` (`FROM qwen3:4b`, temperature 0.1,
  SYSTEM = system_bank + system_w9 prompts, few-shot MESSAGE pairs from
  `prompts/fewshot/`);
* `docker compose exec ollama ollama create mdmdoc-extract -f /mdmdoc-models/Modelfile.mdmdoc-extract`.

The split exists because the app image ships **no `ollama` CLI binary**; on a
bare-metal host with both CLIs the single equivalent repo command is
`mdmdoc train --modelfile --apply`.

`mdmdoc-extract` is intentionally **not** in the bundle — it is rebuilt on the
target so it always matches the deployed repo's prompts and exemplars.

---

## 4. Compose bring-up

```sh
cd <repo>/btp

# 1. Secret — generated locally, never committed, never transferred
umask 177
echo "MDMDOC_API_TOKEN=$(openssl rand -hex 32)" > .env

# 2. Rule-approval store MUST pre-exist as a FILE (single-file bind mount;
#    if missing, Docker would create a directory and approvals could not save)
mkdir -p state
[ -f state/approvals.json ] || echo '{}' > state/approvals.json
# Linux hosts: chown to the container user so the panel can write it.

# 3. Edit compose.full.yaml: replace the MDMDOC_ALLOWED_HOSTS placeholder
#    with the exact hostname the corp proxy forwards (else: 403 forbidden_host).

# 4. Bring up (build needs internet — on a sealed host `docker load` the
#    images first and switch the service from `build:` to `image:`)
docker compose -f compose.full.yaml up -d
```

Then provision models (§3) if not done yet, and point the corp reverse proxy
at `127.0.0.1:8766` (or `127.0.0.1:$MDMDOC_PORT` — set `MDMDOC_PORT` in `.env`
when 8766 is already taken on the host, e.g. by a bare-metal instance).

Key environment (all set in `compose.full.yaml`):

| Var | Value | Why |
|---|---|---|
| `MDMDOC_MODE` | `full` | operator console + teach/train/eval routes |
| `MDMDOC_WEB_EVIDENCE` | `0` | zero egress (§8) |
| `MDMDOC_OLLAMA_HOST` | `http://ollama:11434` | pins the model endpoint; disables all fallback probing incl. the SSH auto-tunnel |
| `MDMDOC_ALLOWED_HOSTS` | corp hostname | FULL-mode Host-header allowlist |
| `MDMDOC_API_TOKEN` | from `.env` | bearer second factor |
| `MDMDOC_BANK_VALUES` | (optional) `masked` | FULL mode defaults to `full` display of account/IBAN/routing for operator verification; set `masked` if corp policy forbids showing them. |
| `MDMDOC_TIN_VALUES` | (optional) `masked` | The same knob for tax numbers (TIN/SSN/EIN). FULL mode defaults to `full`: the operator types them into SAP, and *Download document* hands over the source PDF regardless. Set `masked` if corp policy forbids showing them. Independent of the training-data, egress and `reasoning.md` paths, which never reveal a tax number under any setting. |
| `MDMDOC_RULE_GATE` | default `1` | human rule-approval hard gate; `0` is the no-redeploy off-switch |

---

## 5. Smoke test

```sh
BASE=https://mdmdoc.corp.example.com        # through the proxy
TOKEN=$(grep MDMDOC_API_TOKEN btp/.env | cut -d= -f2)

# liveness (no token needed)
curl -fsS "$BASE/health"

# model connectivity + role resolution
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/doctor"

# upload a document, expect a verdict (synchronous; pipeline runs one at a
# time and a vision run takes 60-180 s → give it >= 300 s)
curl -fsS --max-time 300 -H "Authorization: Bearer $TOKEN" \
     -F "file=@sample-bank-letter.pdf" -F "doc_class=auto" -F "wait=true" \
     "$BASE/api/v1/check"
# → JSON containing a verdict (ACCEPT / REJECT / WARNING / NEED_MANUAL_REVIEW).
#   Fresh install: expect NEED_MANUAL_REVIEW until rules are Approved in the
#   panel — the gate is ON and unreviewed rules force manual review.
#   Async alternative: -F "wait=false" → 202 + job_id, poll /api/v1/jobs/{id}.
```

In a browser (through the proxy, as an SSO user with the operator role):

1. `/ui` loads and lists the run just created.
2. `/ui/rules/approve` is reachable **for the approver role** and shows the
   pending banking/W-9 rules; Approve a rule and re-run the document — the
   verdict firms up.
3. `/ui/training` shows the adoption state (no candidate yet on a fresh
   install).

Expected timing: text-layer check 10–40 s, vision 60–180 s, + SAP compare
20–40 s. One pipeline at a time (server-side lock) — clients must use
`wait=true` with a ≥300 s timeout or poll jobs.

---

## 6. What to back up

| What | Where | Why |
|---|---|---|
| `mdmdoc-inbox` volume | raw uploaded documents | SENSITIVE; erasure unit part 1 |
| `mdmdoc-runs` volume | masked run artifacts | audit trail; erasure unit part 2 |
| `mdmdoc-dataset` volume | `labels.jsonl`, `mlx-lora/` | the teach loop's training labels — irreplaceable operator work |
| `mdmdoc-eval` volume | `history.jsonl`, `report.md` | eval history feeding the adoption gate |
| `mdmdoc-modelfiles` volume | `Modelfile.mdmdoc-extract*`, `adoption.json` | adoption-gate state + rollback Modelfile |
| `mdmdoc-fewshot` volume | `prompts/fewshot/*.json` | rebuilt exemplars (teach loop output) |
| `btp/state/approvals.json` | host bind mount | **live human Approve/Reject decisions** — never clobber, keep out of deploy rsync |
| `btp/.env` | host file | the API token (secret store per corp policy) |

Not worth backing up: the `mdmdoc-ollama` volume (recreatable from the model
bundle + `bundle-models.sh import`) and the containers/images themselves.
Backups of `inbox`/`runs` inherit document sensitivity — store them inside the
same trust boundary. Honoring an erasure request must also purge the backups.

---

## 7. How updates arrive

1. Connected host: `git pull`, rebuild the image
   (`docker build -f btp/Dockerfile -t mdmdoc:latest .`), `docker save` →
   transfer → `docker load` on the sealed host. (If the sealed host may reach
   an internal registry/mirror, `docker compose -f btp/compose.full.yaml build validator`
   works directly.)
2. `docker compose -f btp/compose.full.yaml up -d` — recreates the validator;
   all state lives in the volumes and survives.
3. Rule YAML changes ship inside the image (`rules/banking.yaml`, `w9.yaml`).
   The approval gate binds each decision to the rule's content hash: an
   **edited rule auto-reverts to pending** and must be re-approved at
   `/ui/rules/approve`. Budget an approval pass after every rules update.

   **WARNING — panel rule edits are ephemeral in this deployment.** FULL mode
   exposes a live rules editor (`/ui/rules` and
   `POST /api/v1/rules/{doc_class}/raw`) that writes `rules/*.yaml` — but here
   those files live in the container's **writable layer** (no volume covers
   `rules/`, deliberately, see the `approvals.json` note in
   `compose.full.yaml`), so panel edits are **lost on every container
   recreate** (`up -d` after an image update, `down`, host reboot with
   recreate). In this deployment, durable rule changes go via **git + image
   rebuild** (steps 1–2 above); treat the panel editor as a scratchpad only.
   Shops that want panel-editable rules to persist can bind-mount `./rules`
   into the container instead — with the caveat that the running rules then
   diverge from git and image updates no longer deliver rule changes.
4. If `prompts/` or `prompts/fewshot` seed content changed upstream, rebuild
   the custom model so it matches (§3 build steps, or the UI training flow:
   candidate → eval → Adopt). Base models change rarely; when they do, ship a
   fresh model bundle.
5. Never overwrite `state/approvals.json` or the data volumes during deploys.

Known limitation: training/adoption endpoints that shell out to the `ollama`
CLI (`train --apply`, candidate build, adopt = `ollama cp mdmdoc-extract-candidate
mdmdoc-extract`, rollback) fail inside the stock app container because the
binary is not installed there. Workaround: run the `ollama ...` step from the
ollama container via the shared `/mdmdoc-models` mount (as
`bundle-models.sh import` does), or extend the image with the ollama client
binary if the UI-driven adoption flow must work end to end.

---

## 8. Zero egress

The design goal: **no byte leaves the host** in normal operation.

* **The only internet-touching code** is the external-evidence layer
  (`src/mdmdoc/web_enrichment/` — FDIC BankFind, GLEIF, SEC EDGAR, plus
  operator-hosted connector URLs `MDMDOC_FED_ROUTING_URL` /
  `MDMDOC_SWIFT_LOOKUP_URL`). It is master-gated by `MDMDOC_WEB_EVIDENCE`,
  which this deployment pins to `0`: `web_enrichment.enabled()` is False and
  the ambient evidence path makes **zero calls**. Evidence never affects
  verdicts even when on (NOTE-tier findings only).
* **One documented exception in FULL mode:** the operator console's explicit
  "Verify externally" action sends `web=true`, which bypasses the env flag by
  design (the click *is* the opt-in; in the api-only image the same request is
  rejected with 400). On a sealed network the attempt fails soft into
  "unavailable" hints — but if corp policy forbids even the attempt, keep the
  compose network unable to reach outside (host firewall / egress-deny on the
  Docker bridge), and do not grant the proxy role access to that button's
  route beyond trusted operators.
* **Egress privacy guard:** even when evidence is enabled, every rendered URL
  passes `assert_safe_outbound` before any socket — only routing/ABA numbers,
  SWIFT/BIC codes, bank names and company names may leave; TIN/SSN/EIN,
  account numbers and IBANs raise `EgressBlocked` (nothing sent).
* **No proxy surprises:** the model client's HTTP session runs with
  `trust_env=False` — system/corporate proxy variables are ignored, so
  traffic to Ollama can never be silently routed through a corp proxy (this
  is also why a Cloud-Connector-style proxied topology needs a code change;
  `MDMDOC_HTTP_PROXY` wiring is **not implemented**).
* **No SSH tunnel:** setting `MDMDOC_OLLAMA_HOST` short-circuits host
  resolution before any tunnel probing — the container never spawns `ssh`.
* **Ollama makes no registry calls** at runtime: models arrive via the
  offline bundle; the ollama service publishes no ports and sits on the
  private compose network.
* **Eval is deterministic:** `mdmdoc eval` always runs with web evidence off.
* Remaining egress happens only at **build/provisioning time on the connected
  host** (PyPI/apt/ghcr for the image, `ollama pull` for the bundle) — never
  on the sealed host.

---

## Appendix A — bare-metal variant (no Docker)

Mirrors the reference production pattern (LaunchAgent-supervised
`mdmdoc serve`, which runs uvicorn internally on the app's native default
port 8766).

**Install (macOS host shown; adapt paths for Linux/systemd):**

1. Python ≥ 3.11 and tesseract-ocr + language packs
   (eng/deu/spa/fra/por/rus/chi-sim/kor/jpn/osd) from the corp package
   mirror.
2. From the repo root: `uv sync --frozen --no-dev` (or an equivalent offline
   pip install of the project) so the `mdmdoc` console script is on PATH.
3. Install Ollama natively; start its service; provision models offline:
   `scripts/bundle-models.sh import mdmdoc-models-bundle.tar --store ~/.ollama`
   — this untars the store and runs the real repo command
   `mdmdoc train --modelfile --apply` to build `mdmdoc-extract`.
4. Install the service. The repo's `scripts/install-launchagent.sh` writes
   `~/Library/LaunchAgents/com.egor.mdmdoc.plist` running
   `mdmdoc serve --host 127.0.0.1 --port 8766` with RunAtLoad + KeepAlive and
   log at `~/Library/Logs/mdmdoc-server.log`, but it sets **only PATH** in
   `EnvironmentVariables`. The corp variant must add to that plist dict
   (exactly what the reference prod LaunchAgent `com.victor.mdmdoc` adds, plus
   the pins from §4):

   ```xml
   <key>EnvironmentVariables</key>
   <dict>
     <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
     <key>MDMDOC_API_TOKEN</key><string>REPLACE-long-random-token</string>
     <key>MDMDOC_ALLOWED_HOSTS</key><string>mdmdoc.corp.example.com</string>
     <key>MDMDOC_OLLAMA_HOST</key><string>http://127.0.0.1:11434</string>
     <key>MDMDOC_WEB_EVIDENCE</key><string>0</string>
   </dict>
   ```

   Then `launchctl unload/load` the plist.
5. Keep the bind on `127.0.0.1` and put the corp reverse proxy in front,
   exactly as in §1 (the reference deployment exposes the same thing via
   `tailscale serve` instead).

**Linux equivalent:** a systemd unit whose `ExecStart` is
`mdmdoc serve --host 127.0.0.1 --port 8766`, with the same five environment
variables via `Environment=`, `Restart=always`, and journal logging — verify
unit conventions on the target system.

**Persistence on bare metal:** everything lives under `MDMDOC_HOME` (defaults
to the repo checkout): `runs/`, `inbox/`, `dataset/`, `eval/`, `models/`,
`prompts/fewshot/`, `rules/approvals.json`. Back up per §6; exclude
`rules/approvals.json` from any code-deploy rsync so live decisions are never
clobbered. Updates: `git pull` + `uv sync --frozen --no-dev` + restart the
service; the same rule re-approval note from §7 applies.
