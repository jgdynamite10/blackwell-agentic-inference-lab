#!/usr/bin/env bash
# Idempotent bootstrap for the Akamai RTX PRO 6000 Blackwell baseline host.
#
# Run manually by the operator (root) after provisioning:
#   scp -r infra/akamai/bootstrap <host>:/opt/bwlab-bootstrap
#   ssh <host> 'cd /opt/bwlab-bootstrap && cp bootstrap.env.example bootstrap.env'
#   # fill in the pinned values, then:
#   ssh <host> '/opt/bwlab-bootstrap/bootstrap.sh'
#
# Design (decision D-0012):
# - IDEMPOTENT: every step checks its own outcome before acting; re-running
#   after a failure resumes safely. Marker files under $STATE_DIR record
#   completed steps.
# - PINNED: OS, container image (immutable digest), model artifact revision,
#   and digest manifest are pinned in bootstrap.env; the script REFUSES to
#   serve when a pin is missing.
# - VERIFIED: NVIDIA driver/CUDA compatibility is checked, and every model
#   file is verified against the frozen sha256 manifest BEFORE serving.
# - This script downloads the model and container; it therefore runs ONLY on
#   the provisioned instance in an owner-approved session — never in the
#   hosted Cloud Agent and never in CI.
#
# The watchdog it installs limits runaway WORKLOAD only. It is NOT a billing
# control: on Akamai a powered-off instance still bills. Deleting the
# instance (owner-approved teardown) is the only way to stop charges.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/bootstrap.env"
STATE_DIR="/var/lib/bwlab-bootstrap"

log() { printf '[bwlab-bootstrap] %s\n' "$*"; }
fail() { printf '[bwlab-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

step_done() { [ -f "${STATE_DIR}/$1.done" ]; }
mark_done() { mkdir -p "${STATE_DIR}"; : > "${STATE_DIR}/$1.done"; }

# --- configuration -----------------------------------------------------------

[ -f "${ENV_FILE}" ] || fail "bootstrap.env not found (copy bootstrap.env.example and fill in pins)"
# shellcheck disable=SC1090
. "${ENV_FILE}"

require_pin() {
  local name="$1" value="${2:-}"
  [ -n "${value}" ] || fail "required pin ${name} is empty in bootstrap.env (pins are frozen before serving)"
}

require_pin VLLM_IMAGE "${VLLM_IMAGE:-}"
require_pin MODEL_ARTIFACT "${MODEL_ARTIFACT:-}"
require_pin MODEL_DIR "${MODEL_DIR:-}"
require_pin MODEL_DIGEST_MANIFEST "${MODEL_DIGEST_MANIFEST:-}"
require_pin MIN_DRIVER_BRANCH "${MIN_DRIVER_BRANCH:-}"
require_pin REQUIRED_CUDA_MAJOR "${REQUIRED_CUDA_MAJOR:-}"
require_pin SERVED_MODEL_NAME "${SERVED_MODEL_NAME:-}"
require_pin SERVING_PORT "${SERVING_PORT:-}"
require_pin WATCHDOG_IDLE_MINUTES "${WATCHDOG_IDLE_MINUTES:-}"

# --- step 1: OS assumption check ----------------------------------------------

check_os() {
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID}" = "${REQUIRED_OS_ID}" ] || fail "OS is ${ID}, pinned assumption is ${REQUIRED_OS_ID}"
  case "${VERSION_ID}" in
    "${REQUIRED_OS_VERSION}"*) ;;
    *) fail "OS version is ${VERSION_ID}, pinned assumption is ${REQUIRED_OS_VERSION}" ;;
  esac
  log "OS check passed: ${ID} ${VERSION_ID}"
}

# --- step 2: NVIDIA driver + CUDA compatibility --------------------------------

check_gpu_stack() {
  command -v nvidia-smi >/dev/null 2>&1 \
    || fail "nvidia-smi not found: install the pinned NVIDIA driver (R${MIN_DRIVER_BRANCH}+ branch) first"
  local driver_version driver_branch cuda_version cuda_major gpu_name gpu_count
  driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
  driver_branch="${driver_version%%.*}"
  [ "${driver_branch}" -ge "${MIN_DRIVER_BRANCH}" ] \
    || fail "driver ${driver_version} is older than the pinned minimum branch R${MIN_DRIVER_BRANCH}"
  cuda_version="$(nvidia-smi | sed -n 's/.*CUDA Version: \([0-9][0-9]*\.[0-9]*\).*/\1/p' | head -n1)"
  [ -n "${cuda_version}" ] || fail "could not determine the CUDA version from nvidia-smi"
  cuda_major="${cuda_version%%.*}"
  [ "${cuda_major}" -eq "${REQUIRED_CUDA_MAJOR}" ] \
    || fail "CUDA ${cuda_version} does not match the pinned major version ${REQUIRED_CUDA_MAJOR}.x"
  gpu_count="$(nvidia-smi --list-gpus | wc -l)"
  [ "${gpu_count}" -eq 1 ] || fail "expected exactly 1 GPU, found ${gpu_count} (single-GPU baseline)"
  gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
  case "${gpu_name}" in
    *"RTX PRO 6000"*) ;;
    *) fail "GPU is '${gpu_name}', expected an RTX PRO 6000 Blackwell part" ;;
  esac
  log "GPU stack check passed: ${gpu_name}, driver ${driver_version}, CUDA ${cuda_version}"
}

# --- step 3: container runtime -------------------------------------------------

install_container_runtime() {
  if step_done container-runtime; then
    log "container runtime already installed (marker present)"
    return
  fi
  command -v docker >/dev/null 2>&1 || {
    log "installing docker from Ubuntu 24.04 repositories (pinned distro packages)"
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -q -y docker.io
  }
  command -v nvidia-ctk >/dev/null 2>&1 \
    || fail "nvidia-container-toolkit is not installed; install the NVIDIA-pinned package before rerunning"
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  docker run --rm --gpus all "ubuntu:24.04" true \
    || fail "docker cannot access the GPU through the NVIDIA runtime"
  mark_done container-runtime
  log "container runtime ready (docker + nvidia-container-toolkit)"
}

# --- step 4: serving image by immutable digest ---------------------------------

pull_serving_image() {
  require_pin VLLM_IMAGE_DIGEST "${VLLM_IMAGE_DIGEST:-}"
  case "${VLLM_IMAGE_DIGEST}" in
    sha256:*) ;;
    *) fail "VLLM_IMAGE_DIGEST must be an immutable sha256:... digest (frozen at pilot time)" ;;
  esac
  local pinned_ref="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"
  if docker image inspect "${pinned_ref}" >/dev/null 2>&1; then
    log "serving image already present by digest"
  else
    log "pulling serving image by immutable digest"
    docker pull "${pinned_ref}"
  fi
  log "serving image verified: ${pinned_ref}"
}

# --- step 5: model artifact digest verification ---------------------------------

verify_model_artifact() {
  [ -d "${MODEL_DIR}" ] || fail "model directory ${MODEL_DIR} does not exist (download the pinned artifact first)"
  [ -f "${MODEL_DIGEST_MANIFEST}" ] \
    || fail "digest manifest ${MODEL_DIGEST_MANIFEST} not found: the artifact hash must be frozen before serving"
  log "verifying every model file against the frozen digest manifest (this can take a while)"
  ( cd "${MODEL_DIR}" && sha256sum --check --quiet --strict "${MODEL_DIGEST_MANIFEST}" ) \
    || fail "model artifact digest verification FAILED: refusing to serve unverified weights"
  log "model artifact verified against ${MODEL_DIGEST_MANIFEST}"
}

# --- step 6: launch serving container ------------------------------------------

start_serving() {
  local container_name="bwlab-vllm"
  local pinned_ref="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"
  if docker ps --format '{{.Names}}' | grep -qx "${container_name}"; then
    log "serving container already running"
    return
  fi
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  log "starting vLLM serving container (loopback binding only)"
  # shellcheck disable=SC2086
  docker run -d --name "${container_name}" \
    --gpus all --ipc=host \
    -p "127.0.0.1:${SERVING_PORT}:8000" \
    -v "${MODEL_DIR}:/model:ro" \
    "${pinned_ref}" \
    --model /model \
    --served-model-name "${SERVED_MODEL_NAME}" \
    ${VLLM_EXTRA_ARGS}
}

# --- step 7: health / readiness ------------------------------------------------

wait_for_readiness() {
  local url="http://127.0.0.1:${SERVING_PORT}"
  local deadline=$(( $(date +%s) + 900 ))
  log "waiting for serving readiness at ${url} (max 15 minutes)"
  until curl -fsS "${url}/health" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "${deadline}" ] || fail "serving endpoint did not become healthy within 15 minutes"
    sleep 5
  done
  curl -fsS "${url}/v1/models" | grep -q "${SERVED_MODEL_NAME}" \
    || fail "the served model list does not include ${SERVED_MODEL_NAME}"
  log "serving endpoint is healthy and lists the pinned model"
}

# --- step 8: workload watchdog ---------------------------------------------------

install_watchdog() {
  log "installing workload watchdog (idle limit ${WATCHDOG_IDLE_MINUTES} minutes)"
  install -m 0755 "${SCRIPT_DIR}/watchdog.sh" /usr/local/bin/bwlab-watchdog
  sed "s/@IDLE_MINUTES@/${WATCHDOG_IDLE_MINUTES}/" \
    "${SCRIPT_DIR}/bwlab-watchdog.service.template" > /etc/systemd/system/bwlab-watchdog.service
  install -m 0644 "${SCRIPT_DIR}/bwlab-watchdog.timer" /etc/systemd/system/bwlab-watchdog.timer
  systemctl daemon-reload
  systemctl enable --now bwlab-watchdog.timer
  log "watchdog active. REMINDER: it limits runaway workload only — a powered-off"
  log "Akamai instance STILL BILLS. Owner-approved deletion is the only spending stop."
}

main() {
  check_os
  check_gpu_stack
  install_container_runtime
  pull_serving_image
  verify_model_artifact
  start_serving
  wait_for_readiness
  install_watchdog
  log "bootstrap complete (idempotent: safe to re-run)"
}

main "$@"
