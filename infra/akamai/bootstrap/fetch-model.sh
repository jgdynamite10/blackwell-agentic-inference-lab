#!/usr/bin/env bash
# Credential-safe model acquisition for the pinned baseline artifact.
#
# Run manually by the operator ON THE PROVISIONED INSTANCE, in an
# owner-approved session — never in the hosted Cloud Agent and never in CI.
#
# Credential rules (AGENTS.md sections 3-4; decision D-0013):
# - An access token (if the artifact requires one) is read ONLY from the
#   HF_TOKEN environment variable of the calling shell. It is never accepted
#   as an argument, never echoed, never written to any file, never placed in
#   Terraform state, and never logged. Unset it after use
#   (`unset HF_TOKEN`).
# - This script never enables shell tracing and never prints the
#   environment.
#
# Behavior (idempotent):
# 1. Downloads MODEL_ARTIFACT at the exact pinned MODEL_REVISION into
#    MODEL_DIR (skips the download when the directory already verifies).
# 2. Creates the sha256sum digest manifest at MODEL_DIGEST_MANIFEST when it
#    does not exist yet (first acquisition freezes the digests), or
# 3. Verifies every file against the existing manifest (later runs prove the
#    artifact is byte-identical to the frozen one).

set -euo pipefail
set +x  # defensive: never trace (a trace could echo the environment)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/bootstrap.env"

log() { printf '[bwlab-fetch-model] %s\n' "$*"; }
fail() { printf '[bwlab-fetch-model] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 0 ] || fail "this script takes no arguments (tokens come only from the HF_TOKEN environment variable)"

[ -f "${ENV_FILE}" ] || fail "bootstrap.env not found (copy bootstrap.env.example and fill in pins)"
# shellcheck disable=SC1090
. "${ENV_FILE}"

[ -n "${MODEL_ARTIFACT:-}" ] || fail "MODEL_ARTIFACT pin is empty in bootstrap.env"
[ -n "${MODEL_REVISION:-}" ] || fail "MODEL_REVISION pin is empty in bootstrap.env (the exact revision must be pinned before download)"
[ -n "${MODEL_DIR:-}" ] || fail "MODEL_DIR pin is empty in bootstrap.env"
[ -n "${MODEL_DIGEST_MANIFEST:-}" ] || fail "MODEL_DIGEST_MANIFEST pin is empty in bootstrap.env"

command -v hf >/dev/null 2>&1 || command -v huggingface-cli >/dev/null 2>&1 \
  || fail "the Hugging Face CLI is not installed (pip install 'huggingface_hub[cli]')"

download_model() {
  if [ -d "${MODEL_DIR}" ] && [ -n "$(ls -A "${MODEL_DIR}" 2>/dev/null)" ]; then
    log "model directory already populated; skipping download (verification still runs)"
    return
  fi
  log "downloading ${MODEL_ARTIFACT} at pinned revision ${MODEL_REVISION}"
  # The CLI reads HF_TOKEN from the environment on its own: the token is
  # never passed as an argument, so it cannot appear in `ps`, shell history,
  # or logs.
  mkdir -p "${MODEL_DIR}"
  if command -v hf >/dev/null 2>&1; then
    hf download "${MODEL_ARTIFACT}" --revision "${MODEL_REVISION}" --local-dir "${MODEL_DIR}"
  else
    huggingface-cli download "${MODEL_ARTIFACT}" --revision "${MODEL_REVISION}" --local-dir "${MODEL_DIR}"
  fi
  # Remove CLI cache metadata so the manifest covers model files only.
  rm -rf "${MODEL_DIR}/.cache" "${MODEL_DIR}/.huggingface" 2>/dev/null || true
}

create_or_verify_manifest() {
  if [ -f "${MODEL_DIGEST_MANIFEST}" ]; then
    log "verifying the downloaded artifact against the FROZEN digest manifest"
    ( cd "${MODEL_DIR}" && sha256sum --check --quiet --strict "${MODEL_DIGEST_MANIFEST}" ) \
      || fail "digest verification FAILED: the artifact does not match the frozen manifest"
    log "artifact verified against ${MODEL_DIGEST_MANIFEST}"
    return
  fi
  log "creating the digest manifest (first acquisition freezes the digests)"
  local tmp_manifest
  tmp_manifest="$(mktemp "${MODEL_DIGEST_MANIFEST}.XXXXXX.tmp")"
  ( cd "${MODEL_DIR}" && find . -type f ! -name '*.tmp' -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 sha256sum ) > "${tmp_manifest}" \
    || { rm -f "${tmp_manifest}"; fail "digest manifest creation failed"; }
  chmod 0600 "${tmp_manifest}"
  mv "${tmp_manifest}" "${MODEL_DIGEST_MANIFEST}"
  log "digest manifest written to ${MODEL_DIGEST_MANIFEST} — record and FREEZE it with the baseline"
}

download_model
create_or_verify_manifest
log "done. If HF_TOKEN was set for this session, unset it now: unset HF_TOKEN"
