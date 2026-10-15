#!/bin/bash
# Host-side phases for the D-0046 Akamai NVFP4 cell.
# No tokens on argv or in logs. The operator copies the Hugging Face token
# into /var/lib/bwlab/hf-token (mode 0600) before fetch. This script reads
# that file into HF_TOKEN, runs fetch-model.sh, then deletes the file.
# Akamai keeps billing until the instance is deleted. This script does not
# delete it.
set -euo pipefail

phase="${1:-}"
REPO=/opt/bwlab-repo
BOOT=/opt/bwlab-bootstrap
RESULTS=/var/lib/bwlab/results
TOKEN_FILE=/var/lib/bwlab/hf-token
export BWLAB_BOOTSTRAP_EXAMPLE=bootstrap.nvfp4.env.example

case "${phase}" in
  prepare)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q git curl python3-venv python3-pip ca-certificates
    if [ ! -d "${REPO}/.git" ]; then
      git clone --branch "${BWLAB_GIT_REF}" --depth 1 \
        https://github.com/jgdynamite10/blackwell-agentic-inference-lab.git "${REPO}"
    fi
    rm -rf "${BOOT}"
    cp -a "${REPO}/infra/akamai/bootstrap" "${BOOT}"
    cp "${BOOT}/bootstrap.nvfp4.env.example" "${BOOT}/bootstrap.env"
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
    if [ "${code}" -ne 0 ]; then
      exit "${code}"
    fi
    PYTHONPATH="${REPO}/src" /opt/bwlab-venv/bin/python - << 'PY'
from pathlib import Path
from blackwell_lab.cloud.telemetry import verify_model_artifact
from blackwell_lab.engines.components.vllm_nvfp4 import MODEL_ARTIFACT_HASH

model_dir = Path("/opt/models/nemotron-3.5-lightning-30b-a3b-nvfp4")
manifest = Path("/opt/models/nemotron-3.5-lightning-30b-a3b-nvfp4.sha256")
observed = verify_model_artifact(model_dir, manifest)
if observed != MODEL_ARTIFACT_HASH:
    raise SystemExit("NVFP4 aggregate does not match the D-0046 pin")
print("nvfp4-aggregate-ok")
PY
    ;;
  serve)
    "${BOOT}/bootstrap.sh"
    ;;
  cell)
    export PYTHONPATH="${REPO}/src"
    export LAB_RESULTS_DIR="${RESULTS}"
    /opt/bwlab-venv/bin/python "${REPO}/scripts/akamai_w11_nvfp4_cell.py" "${RESULTS}"
    ;;
  pack)
    tar -C /var/lib/bwlab -cf /var/lib/bwlab/results-export.tar results
    echo "pack-complete"
    ;;
  *)
    echo "usage: akamai_w11_nvfp4_remote.sh prepare|bootstrap|fetch|serve|cell|pack" >&2
    exit 2
    ;;
esac
