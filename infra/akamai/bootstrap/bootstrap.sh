#!/usr/bin/env bash
# Idempotent bootstrap for the Akamai RTX PRO 6000 Blackwell baseline host.
#
# Run manually by the operator (root) after provisioning:
#   scp -r infra/akamai/bootstrap <host>:/opt/bwlab-bootstrap
#   ssh <host> 'cd /opt/bwlab-bootstrap && cp bootstrap.env.example bootstrap.env'
#   # fill in the pinned values, then:
#   ssh <host> '/opt/bwlab-bootstrap/bootstrap.sh'
#
# Design (decisions D-0012, D-0013):
# - IDEMPOTENT: every step checks its own outcome before acting; re-running
#   after a failure (or after the required reboot) resumes safely. Marker
#   files under $STATE_DIR record completed steps.
# - PINNED GPU-STACK ROUTE: the generic Ubuntu image is NOT assumed to ship
#   an NVIDIA driver or the NVIDIA Container Toolkit. This script installs
#   the exact pinned driver and toolkit packages itself, then requires a
#   reboot and a post-reboot re-run that validates the running stack. Exit
#   code 2 means "reboot, then re-run bootstrap.sh".
# - PINNED INPUTS: OS, driver package, container-toolkit package, serving
#   image (immutable digest), GPU probe image (immutable digest), model
#   artifact revision, and digest manifest are pinned in bootstrap.env; the
#   script REFUSES to serve when a pin is missing.
# - VERIFIED: host driver/max-CUDA compatibility AND the container's actual
#   CUDA runtime are checked separately (the nvidia-smi banner is the
#   driver's maximum supported CUDA, not the container runtime), and every
#   model file is verified against the frozen sha256 manifest BEFORE serving.
# - This script downloads the container image; the model is acquired
#   separately by fetch-model.sh. Both run ONLY on the provisioned instance
#   in an owner-approved session — never in the hosted Cloud Agent, never in
#   CI. No token is ever accepted as an argument or written to any file.
#
# The watchdog it installs limits runaway WORKLOAD only. It is NOT a billing
# control: on Akamai a powered-off instance still bills. Deleting the
# instance (owner-approved teardown) is the only way to stop charges.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/bootstrap.env"
STATE_DIR="/var/lib/bwlab-bootstrap"
REBOOT_REQUIRED_EXIT=2

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
require_pin NVIDIA_DRIVER_PACKAGE "${NVIDIA_DRIVER_PACKAGE:-}"
require_pin NVIDIA_DRIVER_PACKAGE_VERSION "${NVIDIA_DRIVER_PACKAGE_VERSION:-}"
require_pin NVIDIA_CTK_PACKAGE_VERSION "${NVIDIA_CTK_PACKAGE_VERSION:-}"
require_pin NVIDIA_REPO_KEY_URL "${NVIDIA_REPO_KEY_URL:-}"
require_pin NVIDIA_REPO_LIST "${NVIDIA_REPO_LIST:-}"
require_pin DOCKER_PACKAGE "${DOCKER_PACKAGE:-}"
require_pin DOCKER_PACKAGE_VERSION "${DOCKER_PACKAGE_VERSION:-}"
require_pin MIN_DRIVER_BRANCH "${MIN_DRIVER_BRANCH:-}"
require_pin DRIVER_MAX_CUDA_MAJOR "${DRIVER_MAX_CUDA_MAJOR:-}"
require_pin GPU_PROBE_IMAGE "${GPU_PROBE_IMAGE:-}"
require_pin GPU_PROBE_EXPECTED_GPU "${GPU_PROBE_EXPECTED_GPU:-}"
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

# --- step 2: pinned NVIDIA driver + container toolkit installation --------------
# Reproducible route (D-0013): install the exact pinned packages when the
# stack is absent, then reboot and re-run for post-reboot validation.

install_gpu_stack() {
  if step_done gpu-stack-installed; then
    log "GPU stack packages already installed (marker present)"
    return
  fi
  local driver_pkg="${NVIDIA_DRIVER_PACKAGE}=${NVIDIA_DRIVER_PACKAGE_VERSION}"
  local ctk_pkg="nvidia-container-toolkit=${NVIDIA_CTK_PACKAGE_VERSION}"
  log "installing pinned GPU stack: ${driver_pkg}, ${ctk_pkg}"
  apt-get update -q
  # One-time NVIDIA apt repository material (pinned URLs in bootstrap.env).
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL "${NVIDIA_REPO_KEY_URL}" | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  echo "${NVIDIA_REPO_LIST}" > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -q -y "${driver_pkg}"
  DEBIAN_FRONTEND=noninteractive apt-get install -q -y "${ctk_pkg}" \
    || fail "nvidia-container-toolkit install failed: verify NVIDIA repo pins in bootstrap.env"
  mark_done gpu-stack-installed
  if ! nvidia-smi >/dev/null 2>&1; then
    log "GPU stack installed but the driver is not active yet."
    log "REBOOT REQUIRED: reboot now, then re-run bootstrap.sh for post-reboot validation."
    exit "${REBOOT_REQUIRED_EXIT}"
  fi
  log "GPU stack installed and driver already active (no reboot needed)"
}

# --- step 3: post-reboot NVIDIA driver + driver-max-CUDA validation -------------

check_gpu_stack() {
  command -v nvidia-smi >/dev/null 2>&1 \
    || fail "nvidia-smi not found after installation: reboot and re-run bootstrap.sh (post-reboot validation)"
  local driver_version driver_branch driver_max_cuda cuda_major gpu_name gpu_count
  driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
  driver_branch="${driver_version%%.*}"
  [ "${driver_branch}" -ge "${MIN_DRIVER_BRANCH}" ] \
    || fail "driver ${driver_version} is older than the pinned minimum branch R${MIN_DRIVER_BRANCH}"
  # The nvidia-smi banner reports the MAXIMUM CUDA version the driver
  # supports — NOT the CUDA runtime any container actually uses. The
  # container runtime is validated separately in check_container_cuda.
  driver_max_cuda="$(nvidia-smi | sed -n 's/.*CUDA Version: \([0-9][0-9]*\.[0-9]*\).*/\1/p' | head -n1)"
  [ -n "${driver_max_cuda}" ] || fail "could not determine the driver's max CUDA version from nvidia-smi"
  cuda_major="${driver_max_cuda%%.*}"
  [ "${cuda_major}" -ge "${DRIVER_MAX_CUDA_MAJOR}" ] \
    || fail "driver max CUDA ${driver_max_cuda} is below the pinned major ${DRIVER_MAX_CUDA_MAJOR}.x"
  gpu_count="$(nvidia-smi --list-gpus | wc -l)"
  [ "${gpu_count}" -eq 1 ] || fail "expected exactly 1 GPU, found ${gpu_count} (single-GPU baseline)"
  gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
  case "${gpu_name}" in
    *"RTX PRO 6000"*) ;;
    *) fail "GPU is '${gpu_name}', expected an RTX PRO 6000 Blackwell part" ;;
  esac
  log "GPU stack validated: ${gpu_name}, driver ${driver_version}, driver-max CUDA ${driver_max_cuda}"
}

# --- step 4: container runtime (docker + NVIDIA runtime + digest-pinned probe) ---

normalize_gpu_match_string() {
  printf '%s' "$1" | tr -d '[:space:]'
}

gpu_probe_name_matches() {
  local observed="$1" expected="$2"
  case "$(normalize_gpu_match_string "${observed}")" in
    *"$(normalize_gpu_match_string "${expected}")"*) return 0 ;;
    *) return 1 ;;
  esac
}

install_container_runtime() {
  if step_done container-runtime; then
    log "container runtime already configured (marker present)"
    return
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log "installing pinned docker runtime: ${DOCKER_PACKAGE}=${DOCKER_PACKAGE_VERSION}"
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -q -y \
      "${DOCKER_PACKAGE}=${DOCKER_PACKAGE_VERSION}"
  fi
  command -v nvidia-ctk >/dev/null 2>&1 \
    || fail "nvidia-ctk not found: install_gpu_stack must complete (and the host reboot) first"
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  require_pin GPU_PROBE_IMAGE_DIGEST "${GPU_PROBE_IMAGE_DIGEST:-}"
  case "${GPU_PROBE_IMAGE_DIGEST}" in
    sha256:*) ;;
    *) fail "GPU_PROBE_IMAGE_DIGEST must be an immutable sha256:... digest (mutable tags are never pulled)" ;;
  esac
  local probe_ref="${GPU_PROBE_IMAGE%%@*}@${GPU_PROBE_IMAGE_DIGEST}"
  local probe_gpu_name probe_gpu_count
  probe_gpu_name="$(docker run --rm --gpus all "${probe_ref}" \
    nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)" \
    || fail "digest-pinned CUDA probe could not run nvidia-smi inside the container"
  probe_gpu_count="$(docker run --rm --gpus all "${probe_ref}" \
    nvidia-smi --list-gpus | wc -l | tr -d '[:space:]')"
  [ "${probe_gpu_count}" = "1" ] \
    || fail "GPU probe expected exactly 1 GPU, found ${probe_gpu_count}"
  if ! gpu_probe_name_matches "${probe_gpu_name}" "${GPU_PROBE_EXPECTED_GPU}"; then
    fail "GPU probe observed '${probe_gpu_name}', expected ${GPU_PROBE_EXPECTED_GPU}"
  fi
  mark_done container-runtime
  log "container runtime ready (docker + nvidia-container-toolkit; digest-pinned CUDA probe passed: ${probe_gpu_name})"
}

# --- step 5: serving image by immutable digest ---------------------------------

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

# --- step 6: container CUDA runtime validation (separate from driver max) --------

check_container_cuda() {
  require_pin REQUIRED_CONTAINER_CUDA_VERSION "${REQUIRED_CONTAINER_CUDA_VERSION:-}"
  local pinned_ref="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"
  local container_cuda
  container_cuda="$(docker run --rm --gpus all --entrypoint python3 "${pinned_ref}" \
    -c 'import torch; print(torch.version.cuda)')" \
    || fail "could not observe the container's CUDA runtime version"
  container_cuda="$(printf '%s' "${container_cuda}" | tail -n1 | tr -d '[:space:]')"
  [ "${container_cuda}" = "${REQUIRED_CONTAINER_CUDA_VERSION}" ] \
    || fail "container CUDA runtime is ${container_cuda}, pinned expectation is ${REQUIRED_CONTAINER_CUDA_VERSION} (the driver's max CUDA is a different fact and does not substitute)"
  log "container CUDA runtime validated: ${container_cuda}"
}

# --- step 7: model artifact digest verification ---------------------------------

verify_model_artifact() {
  [ -d "${MODEL_DIR}" ] || fail "model directory ${MODEL_DIR} does not exist (run fetch-model.sh first)"
  [ -f "${MODEL_DIGEST_MANIFEST}" ] \
    || fail "digest manifest ${MODEL_DIGEST_MANIFEST} not found: the artifact hash must be frozen before serving"
  log "verifying every model file against the frozen digest manifest (this can take a while)"
  ( cd "${MODEL_DIR}" && sha256sum --check --quiet --strict "${MODEL_DIGEST_MANIFEST}" ) \
    || fail "model artifact digest verification FAILED: refusing to serve unverified weights"
  log "model artifact verified against ${MODEL_DIGEST_MANIFEST}"
}

# --- step 8: launch serving container (idempotent restart behavior) --------------

start_serving() {
  local container_name="bwlab-vllm"
  local pinned_ref="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"
  local health_url="http://127.0.0.1:${SERVING_PORT}/health"
  if docker ps --format '{{.Names}}' | grep -qx "${container_name}"; then
    if curl -fsS --max-time 5 "${health_url}" >/dev/null 2>&1; then
      log "serving container already running and healthy"
      return
    fi
    log "serving container is running but not healthy: restarting it"
  fi
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  log "starting vLLM serving container (loopback binding only; never a public port)"
  # shellcheck disable=SC2086
  docker run -d --name "${container_name}" \
    --gpus all --ipc=host \
    --restart no \
    -p "127.0.0.1:${SERVING_PORT}:8000" \
    -v "${MODEL_DIR}:/model:ro" \
    "${pinned_ref}" \
    --model /model \
    --served-model-name "${SERVED_MODEL_NAME}" \
    ${VLLM_EXTRA_ARGS}
}

# --- step 9: health / readiness ------------------------------------------------

wait_for_readiness() {
  local url="http://127.0.0.1:${SERVING_PORT}"
  local deadline=$(( $(date +%s) + 900 ))
  log "waiting for serving readiness at ${url} (max 15 minutes)"
  until curl -fsS "${url}/health" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "${deadline}" ] || fail "serving endpoint did not become healthy within 15 minutes (re-run bootstrap.sh to restart it)"
    sleep 5
  done
  curl -fsS "${url}/v1/models" | grep -q "${SERVED_MODEL_NAME}" \
    || fail "the served model list does not include ${SERVED_MODEL_NAME}"
  log "serving endpoint is healthy and lists the pinned model"
}

# --- step 10: workload watchdog ---------------------------------------------------

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
  install_gpu_stack
  check_gpu_stack
  install_container_runtime
  pull_serving_image
  check_container_cuda
  verify_model_artifact
  start_serving
  wait_for_readiness
  install_watchdog
  log "bootstrap complete (idempotent: safe to re-run)"
}

main "$@"
