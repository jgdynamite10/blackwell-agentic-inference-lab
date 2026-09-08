#!/usr/bin/env bash
# Idempotent bootstrap for the Akamai RTX PRO 6000 Blackwell baseline host.
#
# Run manually by the operator (root) after provisioning:
#   scp -r infra/akamai/bootstrap <host>:/opt/bwlab-bootstrap
#   ssh <host> 'cd /opt/bwlab-bootstrap && cp bootstrap.env.example bootstrap.env'
#   ssh <host> '/opt/bwlab-bootstrap/bootstrap.sh'
#
# Design (decisions D-0012, D-0013, D-0015):
# - VALIDATED FIRST: every mandatory pin is checked against the reviewed
#   candidate baseline BEFORE any apt, curl, gpg, dpkg, or Docker mutation.
# - OPEN KERNEL MODULES: Blackwell requires nvidia-driver-580-server-open.
#   The proprietary nvidia-driver-580-server package is rejected.
# - IDEMPOTENT BUT VERIFIED: markers never skip exact version or key-content
#   checks. A preinstalled wrong version fails or is explicitly converged.
# - PINNED KEY CONTENT: the NVIDIA apt key is downloaded to a temp file,
#   SHA-256 verified, then installed atomically. A mismatch installs nothing.
#
# The watchdog it installs limits runaway WORKLOAD only. It is NOT a billing
# control: on Akamai a powered-off instance still bills.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/bootstrap.env"
EXAMPLE_FILE="${SCRIPT_DIR}/bootstrap.env.example"
STATE_DIR="${BWLAB_BOOTSTRAP_STATE_DIR:-/var/lib/bwlab-bootstrap}"
KEYRING_DIR="${BWLAB_KEYRING_DIR:-/usr/share/keyrings}"
APT_LIST_DIR="${BWLAB_APT_LIST_DIR:-/etc/apt/sources.list.d}"
KEYRING_PATH="${KEYRING_DIR}/nvidia-container-toolkit-keyring.gpg"
APT_LIST_PATH="${APT_LIST_DIR}/nvidia-container-toolkit.list"
REBOOT_REQUIRED_EXIT=2

# shellcheck source=pins.sh
. "${SCRIPT_DIR}/pins.sh"

log() { printf '[bwlab-bootstrap] %s\n' "$*"; }
fail() { printf '[bwlab-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

step_done() { [ -f "${STATE_DIR}/$1.done" ]; }
mark_done() { mkdir -p "${STATE_DIR}"; : > "${STATE_DIR}/$1.done"; }

# Pin validation runs in the executed-script path below, before main() and
# before any privileged mutation. The file can be sourced by tests.

# --- helpers ----------------------------------------------------------------

require_host_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required host command $1 is absent; refusing to mutate the host (will not install an unpinned substitute)"
}

installed_package_version() {
  dpkg-query -W -f='${Version}' "$1" 2>/dev/null || true
}

assert_or_converge_package() {
  local pkg="$1" want="$2" have
  have="$(installed_package_version "${pkg}")"
  if [ "${have}" = "${want}" ]; then
    log "package ${pkg} already at reviewed version ${want}"
    return 0
  fi
  if [ -n "${have}" ]; then
    log "installed ${pkg}=${have} differs from reviewed ${want}; converging explicitly"
  else
    log "installing reviewed package ${pkg}=${want}"
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -q -y "${pkg}=${want}" \
    || fail "apt-get could not install ${pkg}=${want}"
  have="$(installed_package_version "${pkg}")"
  [ "${have}" = "${want}" ] \
    || fail "package ${pkg} is '${have}' after install; reviewed version is ${want} (wrong versions are never accepted)"
}

key_sha256_hex() {
  local value="${NVIDIA_REPO_KEY_SHA256}"
  case "${value}" in
    sha256:*) printf '%s' "${value#sha256:}" ;;
    *) printf '%s' "${value}" ;;
  esac
}

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

check_host_prerequisites() {
  require_host_command curl
  require_host_command gpg
  require_host_command apt-get
  require_host_command dpkg
  require_host_command dpkg-query
  require_host_command sha256sum
  require_host_command uname
  require_host_command modinfo
  local kernel headers_dir build_dir
  kernel="$(uname -r)"
  headers_dir="/usr/src/linux-headers-${kernel}"
  build_dir="/lib/modules/${kernel}/build"
  if [ ! -d "${headers_dir}" ] && [ ! -d "${build_dir}" ]; then
    fail "active-kernel build prerequisites are absent for ${kernel}; install the matching linux-headers package for that exact kernel before bootstrap (refusing to install an unpinned headers metapackage)"
  fi
  log "host commands and active-kernel headers are present for ${kernel}"
}

install_verified_nvidia_key_and_list() {
  require_host_command curl
  require_host_command gpg
  require_host_command sha256sum
  mkdir -p "${KEYRING_DIR}" "${APT_LIST_DIR}"
  local tmp expected
  tmp="$(mktemp)"
  expected="$(key_sha256_hex)"
  if ! curl -fsSL "${NVIDIA_REPO_KEY_URL}" -o "${tmp}"; then
    rm -f "${tmp}"
    fail "NVIDIA repository key download failed; no keyring or apt list was written"
  fi
  if ! printf '%s  %s\n' "${expected}" "${tmp}" | sha256sum --check --strict --status; then
    rm -f "${tmp}"
    fail "NVIDIA repository key SHA-256 mismatch; refusing keyring and apt installation"
  fi
  local tmp_keyring
  tmp_keyring="$(mktemp)"
  if ! gpg --batch --yes --dearmor -o "${tmp_keyring}" "${tmp}"; then
    rm -f "${tmp}" "${tmp_keyring}"
    fail "NVIDIA repository key dearmor failed; no keyring or apt list was written"
  fi
  rm -f "${tmp}"
  install -m 0644 "${tmp_keyring}" "${KEYRING_PATH}"
  rm -f "${tmp_keyring}"
  local tmp_list
  tmp_list="$(mktemp)"
  printf '%s\n' "${NVIDIA_REPO_LIST}" > "${tmp_list}"
  install -m 0644 "${tmp_list}" "${APT_LIST_PATH}"
  rm -f "${tmp_list}"
  [ "$(cat "${APT_LIST_PATH}")" = "${NVIDIA_REPO_LIST}" ] \
    || fail "NVIDIA repository list content does not match the reviewed pin"
  log "NVIDIA repository key content and list verified"
}

verify_installed_gpu_packages() {
  assert_or_converge_package "${NVIDIA_DRIVER_PACKAGE}" "${NVIDIA_DRIVER_PACKAGE_VERSION}"
  local pkg
  for pkg in ${CTK_PACKAGES}; do
    assert_or_converge_package "${pkg}" "${NVIDIA_CTK_PACKAGE_VERSION}"
  done
}

install_gpu_stack() {
  check_host_prerequisites
  # Key content is verified before any apt mutation. A mismatch writes no
  # keyring/list and never reaches apt-get.
  install_verified_nvidia_key_and_list
  if step_done gpu-stack-installed; then
    log "GPU stack marker present; still verifying exact installed versions"
    verify_installed_gpu_packages
    return
  fi
  apt-get update -q
  verify_installed_gpu_packages
  mark_done gpu-stack-installed
  if ! nvidia-smi >/dev/null 2>&1; then
    log "GPU stack installed but the driver is not active yet."
    log "REBOOT REQUIRED: reboot now, then re-run bootstrap.sh for post-reboot validation."
    exit "${REBOOT_REQUIRED_EXIT}"
  fi
  log "GPU stack installed and nvidia-smi is responding; the open-module license gate follows"
}

# --- step 3: post-reboot open-module + GPU identity validation ----------------

check_gpu_stack() {
  command -v nvidia-smi >/dev/null 2>&1 \
    || fail "nvidia-smi not found after installation: reboot and re-run bootstrap.sh (post-reboot validation)"
  local installed
  installed="$(installed_package_version "${NVIDIA_DRIVER_PACKAGE}")"
  [ "${installed}" = "${NVIDIA_DRIVER_PACKAGE_VERSION}" ] \
    || fail "installed ${NVIDIA_DRIVER_PACKAGE}=${installed} is not the reviewed ${NVIDIA_DRIVER_PACKAGE_VERSION}"
  if dpkg-query -W -f='${Status}' "${PROPRIETARY_DRIVER_PACKAGE}" 2>/dev/null | grep -q 'install ok installed'; then
    fail "proprietary ${PROPRIETARY_DRIVER_PACKAGE} is installed; Blackwell requires ${APPROVED_DRIVER_PACKAGE}"
  fi
  local module_license
  # Standard kmod ordering is flags before the module name. Reversing that
  # order can return empty even when the open module is loaded and active.
  module_license="$(modinfo -F license nvidia)" \
    || fail "modinfo could not read the nvidia kernel-module license"
  case "${module_license}" in
    *MIT* | *GPL*) ;;
    *) fail "nvidia kernel-module flavor is not open (license '${module_license}'); expected Dual MIT/GPL from ${APPROVED_DRIVER_PACKAGE}" ;;
  esac
  local driver_version driver_branch driver_max_cuda cuda_major gpu_name gpu_count
  driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
  driver_branch="${driver_version%%.*}"
  [ "${driver_branch}" -ge "${MIN_DRIVER_BRANCH}" ] \
    || fail "driver ${driver_version} is older than the pinned minimum branch R${MIN_DRIVER_BRANCH}"
  driver_max_cuda="$(nvidia-smi | sed -n 's/.*CUDA Version: \([0-9][0-9]*\.[0-9]*\).*/\1/p' | head -n1)"
  [ -n "${driver_max_cuda}" ] || fail "could not determine the driver's max CUDA version from nvidia-smi"
  cuda_major="${driver_max_cuda%%.*}"
  [ "${cuda_major}" -ge "${DRIVER_MAX_CUDA_MAJOR}" ] \
    || fail "driver max CUDA ${driver_max_cuda} is below the pinned major ${DRIVER_MAX_CUDA_MAJOR}.x"
  gpu_count="$(nvidia-smi --list-gpus | wc -l | tr -d '[:space:]')"
  [ "${gpu_count}" = "1" ] || fail "expected exactly 1 GPU, found ${gpu_count} (single-GPU baseline)"
  gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
  case "${gpu_name}" in
    *"RTX PRO 6000"*) ;;
    *) fail "GPU is '${gpu_name}', expected an RTX PRO 6000 Blackwell part" ;;
  esac
  log "GPU stack validated: ${gpu_name}, open ${NVIDIA_DRIVER_PACKAGE}=${installed}, driver ${driver_version}, driver-max CUDA ${driver_max_cuda}"
}

# --- step 4: container runtime ------------------------------------------------

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

verify_installed_docker() {
  assert_or_converge_package "${DOCKER_PACKAGE}" "${DOCKER_PACKAGE_VERSION}"
}

run_gpu_probe() {
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
  log "digest-pinned CUDA probe passed: ${probe_gpu_name}"
}

install_container_runtime() {
  if step_done container-runtime; then
    log "container runtime marker present; still verifying exact Docker and CTK versions"
    verify_installed_gpu_packages
    verify_installed_docker
    return
  fi
  apt-get update -q
  verify_installed_docker
  command -v nvidia-ctk >/dev/null 2>&1 \
    || fail "nvidia-ctk not found: install_gpu_stack must complete (and the host reboot) first"
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  run_gpu_probe
  mark_done container-runtime
  log "container runtime ready (exact docker + nvidia-container-toolkit; digest-pinned CUDA probe passed)"
}

# --- step 5: serving image by immutable digest ---------------------------------

pull_serving_image() {
  local pinned_ref="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"
  if docker image inspect "${pinned_ref}" >/dev/null 2>&1; then
    log "serving image already present by digest"
  else
    log "pulling serving image by immutable digest"
    docker pull "${pinned_ref}"
  fi
  log "serving image verified: ${pinned_ref}"
}

# --- step 6: container CUDA runtime validation --------------------------------

check_container_cuda() {
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

# --- step 7: model artifact digest verification --------------------------------

verify_model_artifact() {
  [ -d "${MODEL_DIR}" ] || fail "model directory ${MODEL_DIR} does not exist (run fetch-model.sh first)"
  [ -f "${MODEL_DIGEST_MANIFEST}" ] \
    || fail "digest manifest ${MODEL_DIGEST_MANIFEST} not found: the artifact hash must be frozen before serving"
  log "verifying every model file against the frozen digest manifest (this can take a while)"
  ( cd "${MODEL_DIR}" && sha256sum --check --quiet --strict "${MODEL_DIGEST_MANIFEST}" ) \
    || fail "model artifact digest verification FAILED: refusing to serve unverified weights"
  log "model artifact verified against ${MODEL_DIGEST_MANIFEST}"
}

# --- step 8: launch serving container ------------------------------------------

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

# --- step 10: workload watchdog ------------------------------------------------

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
  log "bootstrap complete (idempotent: safe to re-run; markers never skip version checks)"
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  load_and_validate_bootstrap_env "${ENV_FILE}" "${EXAMPLE_FILE}"
  main "$@"
fi
