# mdmdoc validation service — api-only image for SAP BTP (CF docker / Kyma).
# Build from the REPO ROOT:  docker build -f btp/Dockerfile -t mdmdoc:latest .
FROM python:3.12-slim

# tesseract + the language packs the corpus needs (EN/DE/ES/FR/PT/RU/zh/ko/ja + OSD)
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-spa \
      tesseract-ocr-fra tesseract-ocr-por tesseract-ocr-rus \
      tesseract-ocr-chi-sim tesseract-ocr-kor tesseract-ocr-jpn tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

RUN useradd -m mdmdoc \
    && mkdir -p /app/runs /app/inbox /app/dataset /app/eval /app/prompts/fewshot \
    && chown -R mdmdoc:mdmdoc /app
USER mdmdoc

# CRITICAL: MDMDOC_HOME must contain rules/, prompts/, templates/ — that is /app.
# Mount persistence volumes at /app/runs and /app/inbox (NOT over /app).
ENV MDMDOC_HOME=/app \
    MDMDOC_MODE=api-only \
    MDMDOC_WEB_EVIDENCE=0 \
    PATH="/app/.venv/bin:$PATH"
# Point at the model endpoint (Ollama-compatible API) at deploy time:
#   MDMDOC_OLLAMA_HOST=http://<model-host>:11434   (see docs/BTP_INTEGRATION.md)
# Set MDMDOC_API_TOKEN — bearer auth is mandatory outside localhost.
# MDMDOC_WEB_EVIDENCE stays 0: the sealed BTP image makes NO outbound registry
# calls. Enable the external-evidence layer only in the local operator console,
# and only with the egress privacy guard (docs/WEB_EVIDENCE.md).

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["mdmdoc", "serve", "--host", "0.0.0.0", "--port", "8080", "--api-only"]
