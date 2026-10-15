#!/bin/bash
# Host-side phases for the D-0047 AWS NVFP4 cell. No tokens on argv or in
# logs. HF_TOKEN is taken from Secrets Manager into the environment for
# fetch-model only, then unset. The 10-hour first-boot timer stays. The
# bootstrap watchdog is disabled after serving is healthy because poweroff
# on this host terminates the instance. This script does not delete it.
set -euo pipefail

phase="${1:-}"
REPO=/opt/bwlab-repo
BOOT=/opt/bwlab-bootstrap
RESULTS=/var/lib/bwlab/results
SECRET_ID="bwlab/aws-w11-nvfp4-20261015a/hf-token"
export BWLAB_BOOTSTRAP_EXAMPLE=bootstrap.nvfp4.env.example

case "${phase}" in
  prepare)
    export DEBIAN_FRONTEND=noninteractive
    if [ -z "${BWLAB_GIT_REF:-}" ] || [ -z "${BWLAB_GIT_COMMIT:-}" ]; then
      echo "BWLAB_GIT_REF and BWLAB_GIT_COMMIT are required" >&2
      exit 1
    fi
    apt-get update -q
    apt-get install -y -q git curl unzip python3-venv python3-pip ca-certificates
    if ! command -v aws >/dev/null 2>&1; then
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
      unzip -q /tmp/awscliv2.zip -d /tmp
      /tmp/aws/install
      rm -rf /tmp/aws /tmp/awscliv2.zip
    fi
    if [ ! -d "${REPO}/.git" ]; then
      git clone --branch "${BWLAB_GIT_REF}" --depth 1 \
        https://github.com/jgdynamite10/blackwell-agentic-inference-lab.git "${REPO}"
    fi
    head="$(git -C "${REPO}" rev-parse HEAD)"
    if [ "${head}" != "${BWLAB_GIT_COMMIT}" ]; then
      echo "cloned commit does not match BWLAB_GIT_COMMIT" >&2
      exit 1
    fi
    if [ -n "$(git -C "${REPO}" status --porcelain)" ]; then
      echo "cloned tree is not clean" >&2
      exit 1
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
    HF_TOKEN="$(aws secretsmanager get-secret-value \
      --secret-id "${SECRET_ID}" \
      --query SecretString \
      --output text \
      --region us-east-1)"
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
    systemctl disable --now bwlab-watchdog.timer
    systemctl stop bwlab-watchdog.service >/dev/null 2>&1 || true
    echo "watchdog-disabled"
    ;;
  cell)
    export PYTHONPATH="${REPO}/src"
    export LAB_RESULTS_DIR="${RESULTS}"
    /opt/bwlab-venv/bin/python "${REPO}/scripts/aws_w11_nvfp4_cell.py" "${RESULTS}"
    ;;
  pack)
    tar -C /var/lib/bwlab -cf /var/lib/bwlab/results-export.tar results
    echo "pack-complete"
    ;;
  *)
    echo "usage: aws_w11_nvfp4_remote.sh prepare|bootstrap|fetch|serve|cell|pack" >&2
    exit 2
    ;;
esac
