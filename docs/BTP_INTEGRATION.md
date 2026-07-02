# SAP BTP Integration Guide

**Scope.** This repo ships everything needed to *integrate later*: a hardened
api-only Docker image, an OpenAPI 3.1 contract (`btp/openapi.json`), Cloud
Foundry and Kyma deployment artifacts, and this guide. **No deployment is
performed from here** — that is a conscious non-goal until the company decides
runtime, model topology and auth.

What the service does for SAP: `POST /api/v1/check` takes a vendor's banking
document (or W-9) and returns `mdmdoc.v1` — doc type, verdict
(ACCEPT/REJECT/WARNING/NEED_MANUAL_REVIEW), extracted masked fields, findings
with rule ids, and (optionally) a field-by-field comparison against SAP Bank
Details. Wire it into the vendor create/update workflow as a pre-MDG gate: an
`invoice` classified as banking support gets kicked back before an MDG reviewer
ever opens the case.

## 1. Build & publish the image

```bash
docker build -f btp/Dockerfile -t <registry>/mdmdoc:<tag> .
docker push <registry>/mdmdoc:<tag>
```

Image facts: python:3.12-slim + tesseract (EN/DE/ES/FR/PT/RU/zh/ko/ja + OSD),
non-root, `MDMDOC_MODE=api-only` (teach loop absent, OpenAPI honest),
HEALTHCHECK on `/health`, listens on 8080. `MDMDOC_HOME=/app` — the image root
carries `rules/`, `prompts/`, `templates/`; **mount volumes at `/app/runs` and
`/app/inbox` only, never over `/app`**. The operator's labels/eval data never
ships in the image (`.dockerignore`).

## 2. The model backend — three topologies (read honestly)

The engine talks the **Ollama API** (`/api/generate` with images and
`format:json`, `/api/tags`, `/api/embeddings`) at `MDMDOC_OLLAMA_HOST`.
It needs one vision model (default `qwen2.5vl:7b`) and one text model
(default `qwen3:4b`, overridable via `MDMDOC_VISION`/`MDMDOC_TEXT`).

| topology | how | status |
|---|---|---|
| **(a) Hybrid: on-prem model via Cloud Connector** | BTP app → Connectivity/Destination → Cloud Connector → on-prem Ollama host. Documents never leave the corporate boundary for inference. | **Gap:** `model_client` uses a direct `requests` session (`trust_env=False`, no proxy support). BTP's connectivity proxy requires routing via `connectivity-proxy` + SAP-CC headers. Requires a small, well-understood code change (`MDMDOC_HTTP_PROXY` env wiring `_SESSION.proxies` + Proxy-Authorization from the connectivity binding). Not implemented yet. |
| **(b) SAP AI Core-hosted LLM** | Serve a vision+text model behind AI Core inference endpoints. | **Gap is real:** AI Core speaks OpenAI-style APIs, not Ollama's (`format:json` enforcement, image transport, `think` flag differ). Pointing `MDMDOC_OLLAMA_HOST` at AI Core does **not** work today. Either run a thin Ollama-façade adapter (translate `/api/generate` → `/v1/chat/completions`; vision parity depends on the served model) or wait for a backend abstraction in `model_client`. **No adapter is promised in this release.** |
| **(c) Ollama sidecar (Kyma)** | Second container in the pod, `MDMDOC_OLLAMA_HOST=http://localhost:11434` (see the commented block in `btp/kyma/deployment.yaml`). | Works with the code as-is. Sizing: qwen2.5vl:7b + qwen3:4b want ~12–16 GiB; CPU-only inference multiplies latency ~5–10× — realistic only on GPU node pools. |

Recommendation: start with **(a)** for a pilot (data stays on-prem, one small
code change), evaluate **(c)** if the cluster has GPUs, treat **(b)** as a
longer-term option.

## 3. Cloud Foundry quickstart

```bash
cf push --var mdmdoc-token=$(openssl rand -hex 24) --var model-host=https://<model-endpoint>
```

`btp/manifest.yml`: docker image path, 2G RAM, http health check on `/health`,
env placeholders. Buildpack alternative (no registry) is documented in the
manifest comments — python buildpack + apt-buildpack for tesseract; docker is
the reproducible path. Keep `instances: 1`: `runs/` history is instance-local
(the verdict itself is stateless — scale is possible if you accept per-instance
run history or mount shared storage).

## 4. Kyma quickstart

```bash
kubectl create ns mdmdoc && kubectl -n mdmdoc create secret generic mdmdoc-secrets \
  --from-literal=api-token=$(openssl rand -hex 24)
kubectl apply -n mdmdoc -f btp/kyma/
```

`deployment.yaml` (probes, resource limits, PVC mounts for `/app/runs` +
`/app/inbox`, commented Ollama sidecar), `service.yaml`, `apirule.yaml`
(exposes the host; switch the access strategy from `noAuth` to `jwt` with your
XSUAA/IAS `jwks_urls` for production).

## 5. Authentication guidance

- Ship-now: `MDMDOC_API_TOKEN` bearer (constant-time compare). Rotate via env.
- Production: put XSUAA in front — CF: AppRouter/route-service validating JWT;
  Kyma: APIRule jwt handler. Keep the bearer token as defense-in-depth behind it.
- The image never renders the token into any HTML (api-only mode has no UI).

## 6. Sizing & performance

| operation | envelope |
|---|---|
| text-layer PDF check | 10–40 s |
| scanned/photo check (vision) | 60–180 s |
| + SAP screenshot comparison | +20–40 s |

One pipeline at a time per instance (`PIPELINE_LOCK`) — by design, matching a
single-model host. Point clients at `wait=true` with a ≥300 s timeout, or
`wait=false` + job polling. Throughput scaling = more instances, each with its
own model endpoint capacity.

## 7. Privacy in BTP (summary — details in PRIVACY.md)

The api-only surface stores: uploaded documents (`/app/inbox`, volume), masked
run artifacts (`/app/runs`). It never persists full account/tax numbers in any
JSON/report; error messages are scrubbed. Erasure = delete `inbox/<sha16>__*`
and `runs/<sha16>/`. Consider `GET /api/v1/runs` exposure vs data-minimization
policy — it returns file names and masked data only, but can be disabled by
fronting rules if needed.

## 8. Limitations (current release)

- Teach loop (labeling/training/eval) is **not** in the BTP image — it stays on
  the operator's Mac; model improvements arrive as updated prompts/fewshot files
  or an updated custom model in a new image tag.
- SAP comparison source is a screenshot upload; replacing it with live Bank
  Details from MDG (OData/BAPI) keeps `sap_compare.compare()` unchanged — only
  the source of the `sap` dict moves from vision to an API call. That is the
  intended BTP integration point.
- Model API is Ollama-only (see topology table).
- No horizontal scale of run history without shared storage.
