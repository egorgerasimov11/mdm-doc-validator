#!/usr/bin/env bash
# =============================================================================
# bundle-models.sh — offline (air-gap) model provisioning for mdmdoc
#
#   export : run on an INTERNET-CONNECTED host. Pulls the exact base models
#            the validator uses, then tars the Ollama store + a SHA-256
#            checksum into a transferable bundle.
#   import : run on the AIR-GAPPED host. Verifies the checksum, untars the
#            store into the Ollama volume (compose deployment) or a local
#            directory (bare metal), then builds the custom `mdmdoc-extract`
#            model from the repo's own Modelfile flow.
#
# Base models (src/mdmdoc/model_client.py roles — do not rename):
#   qwen2.5vl:7b      VISION      — Stage A transcription of scans/images
#   qwen3:4b          TEXT base   — stock base FROM-line of mdmdoc-extract;
#                                   also the runtime fallback until the custom
#                                   model exists
#   qwen3:14b         TEXT_STRONG — escalation tier (--quality / critical gaps)
#   nomic-embed-text  EMBED       — few-shot diversity selection
#
# The custom model `mdmdoc-extract` is NOT shipped in the bundle: it is built
# on the target from qwen3:4b + the repo's prompts (system_bank/system_w9 +
# prompts/fewshot exemplars), so it always matches the deployed repo state.
# Real repo command: `mdmdoc train --modelfile --apply`
#   (--modelfile writes models/Modelfile.mdmdoc-extract; --apply runs
#    `ollama create mdmdoc-extract -f models/Modelfile.mdmdoc-extract`).
# In the compose deployment the app container has no `ollama` CLI binary, so
# import splits that into: write the Modelfile in the validator container,
# then `ollama create` from the ollama container via the shared volume.
# =============================================================================
set -euo pipefail

# ----------------------------------------------------------------------------
# Configuration (override via environment)
# ----------------------------------------------------------------------------
MODELS="${MDMDOC_BUNDLE_MODELS:-qwen2.5vl:7b qwen3:4b qwen3:14b nomic-embed-text}"
BUNDLE="${MDMDOC_BUNDLE:-mdmdoc-models-bundle.tar}"     # tar name (export) / path (import)
OLLAMA_STORE="${OLLAMA_STORE:-$HOME/.ollama}"           # native Ollama store root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/btp/compose.full.yaml}"

usage() {
    cat <<'EOF'
Usage:
  bundle-models.sh export [BUNDLE.tar]
      Run on an internet-connected host with the ollama CLI + a running
      Ollama server. Pulls the required base models:
          qwen2.5vl:7b  qwen3:4b  qwen3:14b  nomic-embed-text
      then tars $OLLAMA_STORE/models into BUNDLE.tar (default
      mdmdoc-models-bundle.tar) and writes BUNDLE.tar.sha256.

  bundle-models.sh import [BUNDLE.tar] [--store DIR]
      Run on the air-gapped host. Verifies BUNDLE.tar against
      BUNDLE.tar.sha256, then:
        (default)       compose deployment: untar into the Ollama named
                        volume wired by btp/compose.full.yaml (always that
                        volume — there is no override), bring the stack
                        up, and build the custom model mdmdoc-extract:
                          docker compose exec validator mdmdoc train --modelfile
                          docker compose exec ollama ollama create \
                              mdmdoc-extract -f /mdmdoc-models/Modelfile.mdmdoc-extract
        --store DIR     bare-metal variant: untar into DIR (an Ollama store
                        root, e.g. ~/.ollama), then build the custom model
                        with the single repo command:
                          mdmdoc train --modelfile --apply

Environment overrides:
  MDMDOC_BUNDLE_MODELS  space-separated model list to pull (export)
  OLLAMA_STORE          native store root for export / --store default
  COMPOSE_FILE          compose file (default <repo>/btp/compose.full.yaml)
EOF
}

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 1; }

sha256_of() {
    # portable SHA-256 (Linux: sha256sum, macOS: shasum)
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "need sha256sum or shasum on PATH"
    fi
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

# ----------------------------------------------------------------------------
# export — internet-connected host
# ----------------------------------------------------------------------------
cmd_export() {
    [ $# -ge 1 ] && BUNDLE="$1"

    command -v ollama >/dev/null 2>&1 || die "ollama CLI not found — install Ollama on this (connected) host first"

    echo "==> Pulling base models (exact names from src/mdmdoc/model_client.py):"
    for m in $MODELS; do
        echo "    ollama pull $m"
        ollama pull "$m"
    done

    [ -d "$OLLAMA_STORE/models" ] || die "no models dir at $OLLAMA_STORE/models — set OLLAMA_STORE to your Ollama store root (contains models/blobs + models/manifests)"

    echo "==> Packing $OLLAMA_STORE/models -> $BUNDLE (uncompressed tar; blobs are already compressed)"
    tar -cf "$BUNDLE" -C "$OLLAMA_STORE" models

    echo "==> Writing checksum"
    sha256_of "$BUNDLE" > "$BUNDLE.sha256"

    echo "==> Done."
    ls -lh "$BUNDLE" "$BUNDLE.sha256"
    cat <<EOF

Transfer BOTH files to the air-gapped host, then run there:
    scripts/bundle-models.sh import $BUNDLE            # compose deployment
    scripts/bundle-models.sh import $BUNDLE --store ~/.ollama   # bare metal
EOF
}

# ----------------------------------------------------------------------------
# import — air-gapped host
# ----------------------------------------------------------------------------
verify_checksum() {
    [ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
    [ -f "$BUNDLE.sha256" ] || die "checksum file not found: $BUNDLE.sha256 (transfer it together with the bundle)"
    echo "==> Verifying checksum"
    expected="$(head -n1 "$BUNDLE.sha256" | awk '{print $1}')"
    actual="$(sha256_of "$BUNDLE")"
    [ "$expected" = "$actual" ] || die "checksum MISMATCH: expected $expected got $actual — the bundle is corrupt or tampered, do not import"
    echo "    OK  $actual"
}

import_volume() {
    command -v docker >/dev/null 2>&1 || die "docker not found"
    [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"

    bundle_dir="$(cd "$(dirname "$BUNDLE")" && pwd)"
    bundle_name="$(basename "$BUNDLE")"

    echo "==> Untarring bundle into the compose file's Ollama named volume (via the ollama service image — no registry access needed)"
    compose run --rm --no-deps \
        -v "$bundle_dir:/bundle:ro" \
        --entrypoint tar \
        ollama -xf "/bundle/$bundle_name" -C /root/.ollama

    echo "==> Starting the stack"
    compose up -d

    echo "==> Waiting for Ollama to answer"
    i=0
    until compose exec -T ollama ollama list >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -gt 30 ] && die "ollama did not come up after 60s — check: docker compose -f $COMPOSE_FILE logs ollama"
        sleep 2
    done

    echo "==> Verifying imported base models"
    tags="$(compose exec -T ollama ollama list)"
    for m in $MODELS; do
        # `ollama list` prints NAME:TAG in column 1; untagged names get :latest
        echo "$tags" | awk '{print $1}' | grep -Fqx -e "$m" -e "$m:latest" \
            || die "model $m missing after import — re-run export (was it pulled there?)"
        echo "    OK  $m"
    done

    echo "==> Building the custom model mdmdoc-extract"
    # Step 1 (repo command): write models/Modelfile.mdmdoc-extract from the
    # deployed prompts + few-shot exemplars. (Equivalent single command on a
    # host with both CLIs: `mdmdoc train --modelfile --apply` — but the app
    # image ships no `ollama` binary, hence the split.)
    compose exec -T validator mdmdoc train --modelfile
    # Step 2: create the model from the ollama container; the validator's
    # /app/models volume is mounted there read-only at /mdmdoc-models.
    compose exec -T ollama ollama create mdmdoc-extract -f /mdmdoc-models/Modelfile.mdmdoc-extract

    echo "==> Verifying mdmdoc-extract"
    compose exec -T ollama ollama list | awk '{print $1}' | grep -Fqx -e "mdmdoc-extract" -e "mdmdoc-extract:latest" \
        || die "mdmdoc-extract not present after create"
    echo "    OK  mdmdoc-extract"

    echo "==> Import complete. Smoke test: see docs/CORP_DEPLOY.md §5."
}

import_store() {
    store="$1"
    mkdir -p "$store"

    echo "==> Untarring bundle into $store"
    tar -xf "$BUNDLE" -C "$store"

    echo "==> Building the custom model mdmdoc-extract (real repo command)"
    command -v mdmdoc >/dev/null 2>&1 || die "mdmdoc CLI not found — install the project first (see docs/CORP_DEPLOY.md appendix)"
    command -v ollama >/dev/null 2>&1 || die "ollama CLI not found on this host"
    # Requires the local Ollama server to be RUNNING and serving $store —
    # the tool never starts Ollama itself.
    mdmdoc train --modelfile --apply

    echo "==> Import complete. Verify with: ollama list  (expect the 4 base models + mdmdoc-extract)"
}

cmd_import() {
    mode="volume"
    target=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --store)  mode="store";  target="${2:?--store needs a directory}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            -*) die "unknown flag: $1" ;;
            *) BUNDLE="$1"; shift ;;
        esac
    done

    verify_checksum
    if [ "$mode" = "volume" ]; then
        import_volume
    else
        import_store "${target:-$OLLAMA_STORE}"
    fi
}

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
[ $# -ge 1 ] || { usage; exit 1; }
cmd="$1"; shift
case "$cmd" in
    export)    cmd_export "$@" ;;
    import)    cmd_import "$@" ;;
    -h|--help|help) usage ;;
    *) usage; die "unknown subcommand: $cmd" ;;
esac
