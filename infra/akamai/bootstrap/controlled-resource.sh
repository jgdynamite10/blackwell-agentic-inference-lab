#!/usr/bin/env bash
# Apply or remove the JOINT 14-vCPU / 100-GiB controlled-resource slice.
#
# The envelope is one shared maximum across the serving container AND the
# benchmark process. Per-container Docker --cpus/--memory flags are refused
# because they would split the joint envelope. Swap is frozen at 0.
#
# Mode transitions recreate the serving container from the already-verified
# local model bind-mount and the digest-pinned image. They never redownload
# weights.
#
# Usage: controlled-resource.sh apply|remove|status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/pins.sh"

SLICE="bwlab-controlled.slice"
CONTAINER="bwlab-vllm"
ACTION="${1:-}"

log() { printf 'bwlab-controlled: %s\n' "$*"; }
fail() { printf 'bwlab-controlled: ERROR: %s\n' "$*" >&2; exit 1; }

require_cgroup_v2() {
  [ -f /sys/fs/cgroup/cgroup.controllers ] \
    || fail "cgroup v2 is required for controlled-resource mode"
}

install_slice() {
  install -m 0644 "${SCRIPT_DIR}/bwlab-controlled.slice" "/etc/systemd/system/${SLICE}"
  systemctl daemon-reload
  systemctl start "${SLICE}"
}

remove_slice() {
  systemctl stop "${SLICE}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SLICE}"
  systemctl daemon-reload
}

restart_serving() {
  local pinned_ref="${VLLM_IMAGE%%@*}@${VLLM_IMAGE_DIGEST}"
  local extra=()
  if [ "${ACTION}" = "apply" ]; then
    extra+=(--cgroup-parent="${SLICE}")
  fi
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  # Do not pass --cpus or --memory: those would apply an independent cap.
  # shellcheck disable=SC2086
  docker run -d --name "${CONTAINER}" \
    --gpus all --ipc=host \
    --restart no \
    "${extra[@]}" \
    -p "127.0.0.1:${SERVING_PORT}:8000" \
    -v "${MODEL_DIR}:/model:ro" \
    "${pinned_ref}" \
    --model /model \
    --served-model-name "${SERVED_MODEL_NAME}" \
    ${VLLM_EXTRA_ARGS}
  log "serving recreated under action=${ACTION} without redownloading the model"
}

wait_health() {
  local url="http://127.0.0.1:${SERVING_PORT}/health"
  local deadline=$(( $(date +%s) + 900 ))
  until curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "${deadline}" ] || fail "serving did not become healthy after the mode transition"
    sleep 5
  done
}

case "${ACTION}" in
  apply)
    require_cgroup_v2
    load_and_validate_bootstrap_env "${SCRIPT_DIR}/bootstrap.env" "${SCRIPT_DIR}/bootstrap.env.example"
    install_slice
    restart_serving
    wait_health
    log "controlled-resource slice applied (joint 14 vCPU / 100 GiB / swap 0)"
    ;;
  remove)
    load_and_validate_bootstrap_env "${SCRIPT_DIR}/bootstrap.env" "${SCRIPT_DIR}/bootstrap.env.example"
    restart_serving
    wait_health
    remove_slice
    log "controlled-resource slice removed; provider-native serving has no residual caps"
    ;;
  status)
    if [ -d "/sys/fs/cgroup/${SLICE}" ]; then
      log "slice present"
    else
      log "slice absent"
    fi
    ;;
  *)
    fail "usage: controlled-resource.sh apply|remove|status"
    ;;
esac
