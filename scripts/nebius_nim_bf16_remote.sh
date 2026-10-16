#!/bin/bash
# Host-side phases for the Nebius NIM BF16 cell.
# The NGC key stays in /var/lib/bwlab/ngc-api-key until the server is
# healthy. NIM downloads the BF16 profile into the container at startup,
# and that download requires the key in the container environment. The
# key is never printed. This script does not start vLLM bootstrap and
# does not install the idle-poweroff watchdog. The 10-hour first-boot
# timer stays. Pack writes results-export-nim.tar.
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
    if ! command -v docker >/dev/null 2>&1; then
      echo "docker-missing" >&2
      exit 1
    fi
    keyfile=/var/lib/bwlab/ngc-api-key
    if [ ! -s "${keyfile}" ]; then
      echo "ngc key file missing" >&2
      exit 1
    fi
    set +x
    NGC_API_KEY="$(cat "${keyfile}")"
    export NGC_API_KEY
    # Registry auth is separate from the container environment. The
    # username is the NGC literal. The key is read from stdin.
    printf '%s' "${NGC_API_KEY}" | docker login nvcr.io --username '$oauthtoken' --password-stdin >/dev/null
    install -d -m 0700 /var/lib/bwlab/nim-cache
    date +%s > /var/lib/bwlab-watchdog.last-active || true
    systemctl disable --now bwlab-watchdog.timer >/dev/null 2>&1 || true
    systemctl stop bwlab-watchdog.service >/dev/null 2>&1 || true
    docker rm -f bwlab-nim >/dev/null 2>&1 || true
    # Pass the variable name only. The value stays in the environment.
    # NIM 2.0.13 does not map tool-choice environment variables onto
    # vLLM. Flags after the image are the nim-serve arguments. The
    # first flag selects that default action.
    docker run -d --name bwlab-nim --gpus all --shm-size=16g \
      -p 127.0.0.1:8000:8000 \
      -e NGC_API_KEY \
      -e NIM_MODEL_PROFILE="${PROFILE}" \
      -v /var/lib/bwlab/nim-cache:/opt/nim/.cache \
      "${IMAGE}" \
      --enable-auto-tool-choice \
      --tool-call-parser qwen3_coder \
      --reasoning-parser nemotron_v3
    unset NGC_API_KEY
    docker logout nvcr.io >/dev/null 2>&1 || true
    ready=0
    # BF16 profile weights are about 63 GB. Allow 90 minutes, and stop
    # early if the container exits.
    for _ in $(seq 1 360); do
      if curl -fsS http://127.0.0.1:8000/v1/models >/tmp/nim-models.json; then
        ready=1
        break
      fi
      running="$(docker inspect -f '{{.State.Running}}' bwlab-nim 2>/dev/null || echo false)"
      if [ "${running}" != "true" ]; then
        echo "nim-container-exited" >&2
        docker logs --tail 40 bwlab-nim 2>&1 | sed -E 's/nvapi-[A-Za-z0-9_-]+/[redacted]/g' >&2 || true
        rm -f "${keyfile}"
        exit 1
      fi
      sleep 15
    done
    rm -f "${keyfile}"
    if [ "${ready}" -ne 1 ]; then
      echo "nim-not-healthy" >&2
      docker logs --tail 40 bwlab-nim 2>&1 | sed -E 's/nvapi-[A-Za-z0-9_-]+/[redacted]/g' >&2 || true
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
