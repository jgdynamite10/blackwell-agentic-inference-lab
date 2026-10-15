#!/bin/bash
# Host-side phases for the D-0045 Nebius cell. No tokens on argv or in logs.
# The operator copies the Hugging Face token into /var/lib/bwlab/hf-token
# (mode 0600) before the fetch phase. This script reads that file into
# HF_TOKEN, runs fetch-model.sh, then deletes the file and unsets the
# variable. The 10-hour first-boot timer stays. The bootstrap watchdog is
# disabled after serving is healthy because a guest shutdown stops compute
# under recovery_policy FAIL; the operator still deletes the instance.
set -euo pipefail

phase="${1:-}"
REPO=/opt/bwlab-repo
BOOT=/opt/bwlab-bootstrap
RESULTS=/var/lib/bwlab/results
TOKEN_FILE=/var/lib/bwlab/hf-token

case "${phase}" in
  prepare)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q git curl unzip python3-venv python3-pip ca-certificates
    if [ ! -d "${REPO}/.git" ]; then
      git clone --branch "${BWLAB_GIT_REF}" --depth 1 \
        https://github.com/jgdynamite10/blackwell-agentic-inference-lab.git "${REPO}"
    fi
    rm -rf "${BOOT}"
    cp -a "${REPO}/infra/akamai/bootstrap" "${BOOT}"
    cp "${BOOT}/bootstrap.env.example" "${BOOT}/bootstrap.env"
    install -d -m 0700 "${RESULTS}" /var/lib/bwlab
    if [ ! -x /opt/bwlab-venv/bin/python ]; then
      python3 -m venv /opt/bwlab-venv
    fi
    /opt/bwlab-venv/bin/pip install -q 'jsonschema[format-nongpl]>=4.21'
    ;;
  bootstrap)
    set +e
    "${BOOT}/bootstrap.sh"
    code=$?
    set -e
    if [ "${code}" -eq 2 ]; then
      echo "reboot-required"
      shutdown -r now
      exit 0
    fi
    exit "${code}"
    ;;
  fetch)
    set +x
    if [ ! -f "${TOKEN_FILE}" ]; then
      echo "token file missing" >&2
      exit 1
    fi
    HF_TOKEN="$(cat "${TOKEN_FILE}")"
    rm -f "${TOKEN_FILE}"
    export HF_TOKEN
    "${BOOT}/fetch-model.sh"
    code=$?
    unset HF_TOKEN
    exit "${code}"
    ;;
  serve)
    "${BOOT}/bootstrap.sh"
    systemctl disable --now bwlab-watchdog.timer
    systemctl stop bwlab-watchdog.service >/dev/null 2>&1 || true
    echo "watchdog-disabled"
    ;;
  cell)
    export PYTHONPATH="${REPO}/src"
    export LAB_RESULTS_DIR="${RESULTS}"
    /opt/bwlab-venv/bin/python "${REPO}/scripts/nebius_w11_full_cell.py" "${RESULTS}"
    ;;
  pack)
    tar -C /var/lib/bwlab -cf /var/lib/bwlab/results-export.tar results
    echo "pack-complete"
    ;;
  *)
    echo "usage: nebius_w11_full_remote.sh prepare|bootstrap|fetch|serve|cell|pack" >&2
    exit 2
    ;;
esac
