# Shared fail-closed pin validation for bootstrap.sh and fetch-model.sh.
# Sourced only; not executed as a program.

# Reviewed candidate baseline: Blackwell requires NVIDIA open kernel modules.
APPROVED_DRIVER_PACKAGE="nvidia-driver-580-server-open"
PROPRIETARY_DRIVER_PACKAGE="nvidia-driver-580-server"
APPROVED_DRIVER_PACKAGE_VERSION="580.173.02-0ubuntu0.24.04.1"
MAX_WATCHDOG_IDLE_MINUTES="45"
CTK_PACKAGES="nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1"

_pin_fail() { printf '[bwlab-pins] ERROR: %s\n' "$*" >&2; exit 1; }

extract_pin() {
  local file="$1" key="$2" line value
  line="$(grep -E "^${key}=" "${file}" 2>/dev/null | tail -n1 || true)"
  [ -n "${line}" ] || return 1
  value="${line#*=}"
  if [ "${#value}" -ge 2 ]; then
    case "${value}" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
  fi
  printf '%s' "${value}"
}

pin_is_floating() {
  local value="$1" lowered
  lowered="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
  case "${lowered}" in
    latest | nightly | stable | current | main | master | head) return 0 ;;
    *:latest | *@latest) return 0 ;;
  esac
  case "${value}" in
    *'*'* | *'?'* | '>='* | '<='* | '>'* | '<'* | '^'*) return 0 ;;
  esac
  return 1
}

pin_is_sha256_digest() {
  local value="$1"
  case "${value}" in
    sha256:[a-fA-F0-9][a-fA-F0-9]*)
      [ "${#value}" -eq 71 ] || return 1
      printf '%s' "${value#sha256:}" | grep -Eq '^[a-fA-F0-9]{64}$'
      ;;
    *) return 1 ;;
  esac
}

pin_is_key_sha256() {
  local value="$1"
  case "${value}" in
    sha256:*) pin_is_sha256_digest "${value}" ;;
    *) printf '%s' "${value}" | grep -Eq '^[a-fA-F0-9]{64}$' ;;
  esac
}

pin_is_revision() {
  printf '%s' "$1" | grep -Eq '^[a-f0-9]{40}$'
}

pin_is_tcp_port() {
  local value="$1"
  case "${value}" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "${value}" -ge 1 ] && [ "${value}" -le 65535 ]
}

pin_is_watchdog_minutes() {
  local value="$1"
  case "${value}" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "${value}" -ge 1 ] && [ "${value}" -le "${MAX_WATCHDOG_IDLE_MINUTES}" ]
}

require_nonempty_pin() {
  local name="$1" value="${2:-}"
  if [ -z "${value}" ]; then
    _pin_fail "required pin ${name} is empty in bootstrap.env (pins are frozen before serving)"
  fi
  if pin_is_floating "${value}"; then
    _pin_fail "required pin ${name} uses a floating, wildcard, or range value"
  fi
}

validate_loaded_pins() {
  require_nonempty_pin VLLM_IMAGE "${VLLM_IMAGE:-}"
  require_nonempty_pin VLLM_IMAGE_DIGEST "${VLLM_IMAGE_DIGEST:-}"
  require_nonempty_pin VLLM_IMAGE_INDEX_DIGEST "${VLLM_IMAGE_INDEX_DIGEST:-}"
  require_nonempty_pin MODEL_ARTIFACT "${MODEL_ARTIFACT:-}"
  require_nonempty_pin MODEL_REVISION "${MODEL_REVISION:-}"
  require_nonempty_pin MODEL_DIR "${MODEL_DIR:-}"
  require_nonempty_pin MODEL_DIGEST_MANIFEST "${MODEL_DIGEST_MANIFEST:-}"
  require_nonempty_pin NVIDIA_DRIVER_PACKAGE "${NVIDIA_DRIVER_PACKAGE:-}"
  require_nonempty_pin NVIDIA_DRIVER_PACKAGE_VERSION "${NVIDIA_DRIVER_PACKAGE_VERSION:-}"
  require_nonempty_pin NVIDIA_CTK_PACKAGE_VERSION "${NVIDIA_CTK_PACKAGE_VERSION:-}"
  require_nonempty_pin NVIDIA_REPO_KEY_URL "${NVIDIA_REPO_KEY_URL:-}"
  require_nonempty_pin NVIDIA_REPO_KEY_SHA256 "${NVIDIA_REPO_KEY_SHA256:-}"
  require_nonempty_pin NVIDIA_REPO_LIST "${NVIDIA_REPO_LIST:-}"
  require_nonempty_pin DOCKER_PACKAGE "${DOCKER_PACKAGE:-}"
  require_nonempty_pin DOCKER_PACKAGE_VERSION "${DOCKER_PACKAGE_VERSION:-}"
  require_nonempty_pin MIN_DRIVER_BRANCH "${MIN_DRIVER_BRANCH:-}"
  require_nonempty_pin DRIVER_MAX_CUDA_MAJOR "${DRIVER_MAX_CUDA_MAJOR:-}"
  require_nonempty_pin REQUIRED_CONTAINER_CUDA_VERSION "${REQUIRED_CONTAINER_CUDA_VERSION:-}"
  require_nonempty_pin GPU_PROBE_IMAGE "${GPU_PROBE_IMAGE:-}"
  require_nonempty_pin GPU_PROBE_IMAGE_DIGEST "${GPU_PROBE_IMAGE_DIGEST:-}"
  require_nonempty_pin GPU_PROBE_IMAGE_INDEX_DIGEST "${GPU_PROBE_IMAGE_INDEX_DIGEST:-}"
  require_nonempty_pin GPU_PROBE_EXPECTED_GPU "${GPU_PROBE_EXPECTED_GPU:-}"
  require_nonempty_pin REQUIRED_OS_ID "${REQUIRED_OS_ID:-}"
  require_nonempty_pin REQUIRED_OS_VERSION "${REQUIRED_OS_VERSION:-}"
  require_nonempty_pin SERVED_MODEL_NAME "${SERVED_MODEL_NAME:-}"
  require_nonempty_pin SERVING_PORT "${SERVING_PORT:-}"
  require_nonempty_pin VLLM_EXTRA_ARGS "${VLLM_EXTRA_ARGS:-}"
  require_nonempty_pin WATCHDOG_IDLE_MINUTES "${WATCHDOG_IDLE_MINUTES:-}"

  pin_is_sha256_digest "${VLLM_IMAGE_DIGEST}" \
    || _pin_fail "VLLM_IMAGE_DIGEST must be an immutable sha256:<64-hex> digest"
  pin_is_sha256_digest "${VLLM_IMAGE_INDEX_DIGEST}" \
    || _pin_fail "VLLM_IMAGE_INDEX_DIGEST must be an immutable sha256:<64-hex> digest"
  pin_is_sha256_digest "${GPU_PROBE_IMAGE_DIGEST}" \
    || _pin_fail "GPU_PROBE_IMAGE_DIGEST must be an immutable sha256:<64-hex> digest"
  pin_is_sha256_digest "${GPU_PROBE_IMAGE_INDEX_DIGEST}" \
    || _pin_fail "GPU_PROBE_IMAGE_INDEX_DIGEST must be an immutable sha256:<64-hex> digest"
  pin_is_key_sha256 "${NVIDIA_REPO_KEY_SHA256}" \
    || _pin_fail "NVIDIA_REPO_KEY_SHA256 must be the 64-hex SHA-256 of the official armored key"
  pin_is_revision "${MODEL_REVISION}" \
    || _pin_fail "MODEL_REVISION must be the exact 40-character Hugging Face commit"

  [ "${NVIDIA_DRIVER_PACKAGE}" = "${APPROVED_DRIVER_PACKAGE}" ] \
    || _pin_fail "NVIDIA_DRIVER_PACKAGE must be ${APPROVED_DRIVER_PACKAGE} (Blackwell requires open kernel modules)"
  [ "${NVIDIA_DRIVER_PACKAGE}" != "${PROPRIETARY_DRIVER_PACKAGE}" ] \
    || _pin_fail "proprietary ${PROPRIETARY_DRIVER_PACKAGE} is rejected; use ${APPROVED_DRIVER_PACKAGE}"
  [ "${NVIDIA_DRIVER_PACKAGE_VERSION}" = "${APPROVED_DRIVER_PACKAGE_VERSION}" ] \
    || _pin_fail "NVIDIA_DRIVER_PACKAGE_VERSION must equal the reviewed candidate ${APPROVED_DRIVER_PACKAGE_VERSION}"
  if ! pin_is_tcp_port "${SERVING_PORT}"; then
    _pin_fail "SERVING_PORT must be an integer in 1-65535"
  fi
  if ! pin_is_watchdog_minutes "${WATCHDOG_IDLE_MINUTES}"; then
    _pin_fail "WATCHDOG_IDLE_MINUTES must be a positive integer no greater than ${MAX_WATCHDOG_IDLE_MINUTES}"
  fi
}

validate_env_matches_example() {
  local example="$1"
  local key expected actual
  [ -f "${example}" ] || _pin_fail "reviewed candidate baseline ${example} is missing"
  for key in \
    VLLM_IMAGE VLLM_IMAGE_DIGEST VLLM_IMAGE_INDEX_DIGEST \
    MODEL_ARTIFACT MODEL_REVISION \
    NVIDIA_DRIVER_PACKAGE NVIDIA_DRIVER_PACKAGE_VERSION \
    NVIDIA_CTK_PACKAGE_VERSION NVIDIA_REPO_KEY_URL NVIDIA_REPO_KEY_SHA256 \
    NVIDIA_REPO_LIST DOCKER_PACKAGE DOCKER_PACKAGE_VERSION \
    MIN_DRIVER_BRANCH DRIVER_MAX_CUDA_MAJOR REQUIRED_CONTAINER_CUDA_VERSION \
    GPU_PROBE_IMAGE GPU_PROBE_IMAGE_DIGEST GPU_PROBE_IMAGE_INDEX_DIGEST \
    GPU_PROBE_EXPECTED_GPU \
    REQUIRED_OS_ID REQUIRED_OS_VERSION SERVED_MODEL_NAME SERVING_PORT \
    VLLM_EXTRA_ARGS WATCHDOG_IDLE_MINUTES; do
    expected="$(extract_pin "${example}" "${key}")" \
      || _pin_fail "reviewed candidate baseline is missing ${key}"
    eval "actual=\"\${${key}:-}\""
    [ "${actual}" = "${expected}" ] \
      || _pin_fail "bootstrap.env ${key} differs from the reviewed candidate baseline"
  done
  # Live hosts must use the reviewed model paths. Tests may override them
  # only by setting BWLAB_ALLOW_PATH_OVERRIDE=1.
  if [ "${BWLAB_ALLOW_PATH_OVERRIDE:-}" != "1" ]; then
    for key in MODEL_DIR MODEL_DIGEST_MANIFEST; do
      expected="$(extract_pin "${example}" "${key}")" \
        || _pin_fail "reviewed candidate baseline is missing ${key}"
      eval "actual=\"\${${key}:-}\""
      [ "${actual}" = "${expected}" ] \
        || _pin_fail "bootstrap.env ${key} differs from the reviewed candidate baseline"
    done
  fi
}

load_and_validate_bootstrap_env() {
  local env_file="$1" example_file="$2"
  [ -f "${env_file}" ] || _pin_fail "bootstrap.env not found (copy bootstrap.env.example and fill in pins)"
  # shellcheck disable=SC1090
  set -a
  # shellcheck disable=SC1090
  . "${env_file}"
  set +a
  validate_loaded_pins
  validate_env_matches_example "${example_file}"
}
