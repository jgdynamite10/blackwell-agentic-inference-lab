#!/bin/bash
# Host-side phases for the Nebius NIM BF16 cell.
# The NGC key is copied to /var/lib/bwlab/ngc-api-key only for the image
# pull, then deleted. This script does not start vLLM bootstrap and does
# not install the idle-poweroff watchdog. The 10-hour first-boot timer
# stays. Pack writes results-export-nim.tar.
set -euo pipefail

phase="${1:-}"
REPO=/opt/bwlab-repo
RESULTS=/var/lib/bwlab/results
IMAGE=nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:4f93f7e217ca05954bb34d3ccabbb3080f419e0ee5e1c0053f70debc00eb97e8
PROFILE=d74ba8ea77c6fa3a553400b56e1172846a3c073b0c57c2f24f3d48e71184fa39

case "${phase}" in
  prepare)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q git curl ca-certificates python3-venv python3-pip
    if [ ! -d "${REPO}/.git" ]; then
      git clone --branch "${BWLAB_GIT_REF}" --depth 1 \
        https://github.com/jgdynamite10/blackwell-agentic-inference-lab.git "${REPO}"
    fi
    git -C "${REPO}" rev-parse HEAD
    git -C "${REPO}" status --porcelain
    install -d -m 0700 "${RESULTS}" /var/lib/bwlab
    if [ ! -x /opt/bwlab-venv/bin/python ]; then
      python3 -m venv /opt/bwlab-venv
    fi
    /opt/bwlab-venv/bin/pip install -q 'jsonschema[format-nongpl]>=4.21'
    ;;
  serve)
    date +%s > /var/lib/bwlab-watchdog.last-active || true
    systemctl disable --now bwlab-watchdog.timer >/dev/null 2>&1 || true
    systemctl stop bwlab-watchdog.service >/dev/null 2>&1 || true
    docker rm -f bwlab-nim >/dev/null 2>&1 || true
    docker run -d --name bwlab-nim --gpus all --shm-size=16g \
      -p 127.0.0.1:8000:8000 \
      -e NIM_MODEL_PROFILE="${PROFILE}" \
      "${IMAGE}"
    ready=0
    for _ in $(seq 1 120); do
      if curl -fsS http://127.0.0.1:8000/v1/models >/tmp/nim-models.json; then
        ready=1
        break
      fi
      sleep 15
    done
    if [ "${ready}" -ne 1 ]; then
      echo "nim-not-healthy" >&2
      exit 1
    fi
    python3 - << 'PY'
import json, urllib.request
body = json.dumps({
    "model": json.load(open("/tmp/nim-models.json"))["data"][0]["id"],
    "messages": [{"role": "user", "content": "Call get_service_health for the api service."}],
    "temperature": 0.2,
    "top_p": 0.95,
    "max_tokens": 128,
    "stream": False,
    "tool_choice": "auto",
    "tools": [{
        "type": "function",
        "function": {
            "name": "get_service_health",
            "description": "Return synthetic health for one service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    }],
}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8000/v1/chat/completions",
    data=body,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=180) as response:
    payload = json.load(response)
message = payload["choices"][0]["message"]
if not message.get("tool_calls"):
    raise SystemExit("native-tool-calls-missing")
print("nim-tools-ok")
PY
    if systemctl is-active bwlab-watchdog.timer >/dev/null 2>&1; then
      echo "watchdog-still-active" >&2
      exit 1
    fi
    echo "watchdog-disabled"
    ;;
  cell)
    export PYTHONPATH="${REPO}/src"
    export PYTHONUNBUFFERED=1
    export LAB_RESULTS_DIR="${RESULTS}"
    /opt/bwlab-venv/bin/python "${REPO}/scripts/nebius_nim_bf16_cell.py" "${RESULTS}"
    ;;
  pack)
    tar -C /var/lib/bwlab -cf /var/lib/bwlab/results-export-nim.tar results
    echo "pack-complete"
    ;;
  *)
    echo "usage: nebius_nim_bf16_remote.sh prepare|serve|cell|pack" >&2
    exit 2
    ;;
esac
