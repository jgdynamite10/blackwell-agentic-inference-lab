#!/bin/bash
# Host-side phases for the D-0044 AWS cell. No tokens on argv or in logs.
# HF_TOKEN is taken from Secrets Manager into the environment for fetch-model
# only, then unset. The 10-hour first-boot timer is left in place. The
# bootstrap watchdog is disabled after serving is healthy because poweroff
# on this host terminates the instance.
set -euo pipefail

phase="${1:-}"
REPO=/opt/bwlab-repo
BOOT=/opt/bwlab-bootstrap
RESULTS=/var/lib/bwlab/results

case "${phase}" in
  prepare)
    export DEBIAN_FRONTEND=noninteractive
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
    rm -rf "${BOOT}"
    cp -a "${REPO}/infra/akamai/bootstrap" "${BOOT}"
    cp "${BOOT}/bootstrap.env.example" "${BOOT}/bootstrap.env"
    install -d -m 0700 "${RESULTS}"
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
      --secret-id "${BWLAB_SECRET_ID}" \
      --query SecretString \
      --output text \
      --region us-east-1)"
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
    /opt/bwlab-venv/bin/python "${REPO}/scripts/aws_w11_full_cell.py" "${RESULTS}"
    ;;
  upload)
    aws s3 sync "${RESULTS}" "s3://${BWLAB_RESULTS_BUCKET}/results" \
      --region us-east-1 --only-show-errors
    echo "upload-complete"
    ;;
  *)
    echo "usage: aws_w11_full_remote.sh prepare|bootstrap|fetch|serve|cell|upload" >&2
    exit 2
    ;;
esac
