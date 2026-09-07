#!/usr/bin/env bash
# Credential-safe, atomically promoted model acquisition.
#
# Uses the digest-pinned vLLM image (overridden entrypoint) — never an
# unpinned host Hugging Face CLI install.
#
# Credential rules (AGENTS.md sections 3-4; decision D-0013):
# - HF_TOKEN, if required, is passed to the container only as an inherited
#   environment variable name (`-e HF_TOKEN`). It is never accepted as an
#   argument, never expanded into argv, never echoed, and never written to
#   a file. Unset it after use (`unset HF_TOKEN`).
#
# Acquisition rules:
# - download the exact MODEL_REVISION into a revision-specific staging dir;
# - treat an unmanifested MODEL_DIR as incomplete/untrusted;
# - never skip download merely because MODEL_DIR is nonempty;
# - write the per-file SHA-256 manifest only after a successful download;
# - atomically promote staging to MODEL_DIR;
# - verify an existing manifest before reuse.

set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/bootstrap.env"
EXAMPLE_FILE="${SCRIPT_DIR}/bootstrap.env.example"

# shellcheck source=pins.sh
. "${SCRIPT_DIR}/pins.sh"

log() { printf '[bwlab-fetch-model] %s\n' "$*"; }
fail() { printf '[bwlab-fetch-model] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 0 ] || fail "this script takes no arguments (tokens come only from the HF_TOKEN environment variable)"

load_and_validate_bootstrap_env "${ENV_FILE}" "${EXAMPLE_FILE}"

STAGE_DIR="${MODEL_DIR}.rev-${MODEL_REVISION}.staging"
PINNED_REF="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"

manifest_verifies() {
  local directory="$1" manifest="$2"
  [ -d "${directory}" ] || return 1
  [ -f "${manifest}" ] || return 1
  ( cd "${directory}" && sha256sum --check --quiet --strict "${manifest}" )
}

quarantine_untrusted_model_dir() {
  local stamp dest
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dest="${MODEL_DIR}.untrusted-${stamp}"
  log "existing MODEL_DIR is unmanifested or does not match the manifest; quarantining as untrusted"
  mv "${MODEL_DIR}" "${dest}"
  rm -f "${MODEL_DIGEST_MANIFEST}"
}

write_manifest_from() {
  local source_dir="$1" dest="$2" tmp found
  tmp="$(mktemp "${dest}.XXXXXX")"
  found=0
  (
    cd "${source_dir}" || exit 1
    while IFS= read -r rel; do
      [ -f "${rel}" ] || continue
      sha256sum "${rel}" || exit 1
      found=1
    done < <(find . -type f ! -name '*.tmp' ! -path './.cache/*' ! -path './.huggingface/*' | LC_ALL=C sort)
    [ "${found}" = 1 ]
  ) > "${tmp}" \
    || { rm -f "${tmp}"; fail "digest manifest creation failed; leaving the final directory uncertified"; }
  [ -s "${tmp}" ] || { rm -f "${tmp}"; fail "digest manifest would be empty; refusing to certify"; }
  chmod 0600 "${tmp}"
  mv "${tmp}" "${dest}"
}

download_revision_into_staging() {
  command -v docker >/dev/null 2>&1 || fail "docker is required; fetch-model uses the digest-pinned vLLM image, not a host Hugging Face CLI"
  if ! docker image inspect "${PINNED_REF}" >/dev/null 2>&1; then
    log "pulling digest-pinned acquisition image ${PINNED_REF}"
    docker pull "${PINNED_REF}"
  fi
  mkdir -p "${STAGE_DIR}"
  log "downloading ${MODEL_ARTIFACT} at exact revision ${MODEL_REVISION} into staging"
  # -e HF_TOKEN passes the variable NAME only. The value is never on argv.
  docker run --rm \
    --entrypoint python3 \
    -e HF_TOKEN \
    -e MODEL_ARTIFACT \
    -e MODEL_REVISION \
    -e MODEL_STAGE_DIR="${STAGE_DIR}" \
    -v "${STAGE_DIR}:${STAGE_DIR}" \
    "${PINNED_REF}" \
    -c 'from huggingface_hub import snapshot_download
import os
snapshot_download(
    repo_id=os.environ["MODEL_ARTIFACT"],
    revision=os.environ["MODEL_REVISION"],
    local_dir=os.environ["MODEL_STAGE_DIR"],
)
' || fail "exact-revision download failed; staging is incomplete and no manifest was written"
  rm -rf "${STAGE_DIR}/.cache" "${STAGE_DIR}/.huggingface" 2>/dev/null || true
  [ -n "$(ls -A "${STAGE_DIR}" 2>/dev/null)" ] \
    || fail "staging directory is empty after download; refusing to certify"
}

promote_staging() {
  write_manifest_from "${STAGE_DIR}" "${MODEL_DIGEST_MANIFEST}"
  mkdir -p "$(dirname "${MODEL_DIR}")"
  if [ -e "${MODEL_DIR}" ]; then
    fail "MODEL_DIR exists during promote; refusing to overwrite a directory that was not quarantined"
  fi
  mv "${STAGE_DIR}" "${MODEL_DIR}"
  manifest_verifies "${MODEL_DIR}" "${MODEL_DIGEST_MANIFEST}" \
    || fail "promoted directory failed digest verification; refusing to treat it as complete"
  log "atomically promoted staging to ${MODEL_DIR} and certified the digest manifest"
}

acquire_model() {
  if [ -d "${MODEL_DIR}" ] && manifest_verifies "${MODEL_DIR}" "${MODEL_DIGEST_MANIFEST}"; then
    log "completed download already verifies against the digest manifest; reusing it"
    return
  fi
  if [ -d "${MODEL_DIR}" ]; then
    quarantine_untrusted_model_dir
  elif [ -f "${MODEL_DIGEST_MANIFEST}" ]; then
    log "digest manifest exists without a matching MODEL_DIR; removing the uncertified manifest"
    rm -f "${MODEL_DIGEST_MANIFEST}"
  fi
  download_revision_into_staging
  promote_staging
}

acquire_model
log "done. If HF_TOKEN was set for this session, unset it now: unset HF_TOKEN"
