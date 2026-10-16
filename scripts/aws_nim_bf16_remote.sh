#!/bin/bash
# Host-side phases for the AWS NIM BF16 cell on the running NVFP4 host.
# sync refuses to start until the NVFP4 result file exists and vLLM has
# stopped. The NGC key stays in /var/lib/bwlab/ngc-api-key until the
# server is healthy. It is never printed. Pack writes
# results-export-nim.tar and leaves the NVFP4 archive in place.
set -euo pipefail

phase="${1:-}"
REPO=/opt/bwlab-repo
RESULTS=/var/lib/bwlab/results
IMAGE=nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:4f93f7e217ca05954bb34d3ccabbb3080f419e0ee5e1c0053f70debc00eb97e8
PROFILE=d74ba8ea77c6fa3a553400b56e1172846a3c073b0c57c2f24f3d48e71184fa39

nvfp4_finished() {
  find "${RESULTS}/real-runs/aws-w11-nvfp4-20261015a" -name '*.result.json' -type f | grep -q .
}

case "${phase}" in
  sync)
    if [ -z "${BWLAB_GIT_REF:-}" ] || [ -z "${BWLAB_GIT_COMMIT:-}" ]; then
      echo "BWLAB_GIT_REF and BWLAB_GIT_COMMIT are required" >&2
      exit 1
    fi
    if ! nvfp4_finished; then
      echo "nvfp4-result-missing" >&2
      exit 1
    fi
    if docker ps --format '{{.Names}}' | grep -qx bwlab-vllm; then
      echo "vllm-still-running" >&2
      exit 1
    fi
    git -C "${REPO}" fetch --depth 1 origin "${BWLAB_GIT_REF}"
    git -C "${REPO}" checkout --detach FETCH_HEAD
    head="$(git -C "${REPO}" rev-parse HEAD)"
    if [ "${head}" != "${BWLAB_GIT_COMMIT}" ]; then
      echo "cloned commit does not match BWLAB_GIT_COMMIT" >&2
      exit 1
    fi
    if [ -n "$(git -C "${REPO}" status --porcelain)" ]; then
      echo "cloned tree is not clean" >&2
      exit 1
    fi
    echo "nim-sync-ok ${head}"
    ;;
  serve)
    if ! nvfp4_finished; then
      echo "nvfp4-result-missing" >&2
      exit 1
    fi
    if ! command -v docker >/dev/null 2>&1; then
      echo "docker-missing" >&2
      exit 1
    fi
    keyfile=/var/lib/bwlab/ngc-api-key
    # The measured NIM process is uid 1000, gid 0. Mode 0700 is root-only,
    # so the container exits before it can write the profile cache. Group
    # write plus setgid keeps files created by that process in group 0.
    install -d -o root -g 0 -m 2770 /var/lib/bwlab/nim-cache
    chgrp 0 /var/lib/bwlab/nim-cache
    chmod 2770 /var/lib/bwlab/nim-cache
    date +%s > /var/lib/bwlab-watchdog.last-active || true
    systemctl disable --now bwlab-watchdog.timer >/dev/null 2>&1 || true
    systemctl stop bwlab-watchdog.service >/dev/null 2>&1 || true
    nim_command="$(docker inspect -f '{{json .Config.Cmd}}{{json .Args}}' bwlab-nim 2>/dev/null || true)"
    already=0
    case "${nim_command}" in
      *nim-serve*--enable-auto-tool-choice*qwen3_coder*nemotron_v3*)
        if [ "$(docker inspect -f '{{.State.Running}}' bwlab-nim 2>/dev/null || echo false)" = "true" ]; then
          already=1
          echo "nim-already-serving"
        fi
        ;;
    esac
    if [ "${already}" -ne 1 ]; then
      if [ ! -s "${keyfile}" ]; then
        echo "ngc key file missing" >&2
        exit 1
      fi
      docker rm -f bwlab-vllm >/dev/null 2>&1 || true
      set +x
      NGC_API_KEY="$(cat "${keyfile}")"
      export NGC_API_KEY
      printf '%s' "${NGC_API_KEY}" | docker login nvcr.io --username '$oauthtoken' --password-stdin >/dev/null
      docker rm -f bwlab-nim >/dev/null 2>&1 || true
    # NIM 2.0.13 does not map tool-choice environment variables onto
    # vLLM. The documented command is nim-serve plus these arguments.
    docker run -d --name bwlab-nim --gpus all --shm-size=16g \
      -p 127.0.0.1:8000:8000 \
      -e NGC_API_KEY \
      -e NIM_MODEL_PROFILE="${PROFILE}" \
      -v /var/lib/bwlab/nim-cache:/opt/nim/.cache \
      "${IMAGE}" \
      nim-serve \
      --enable-auto-tool-choice \
      --tool-call-parser qwen3_coder \
      --reasoning-parser nemotron_v3
      unset NGC_API_KEY
      docker logout nvcr.io >/dev/null 2>&1 || true
    fi
    ready=0
    for _ in $(seq 1 360); do
      if curl -fsS http://127.0.0.1:8000/v1/models >/tmp/nim-models.json; then
        ready=1
        break
      fi
      running="$(docker inspect -f '{{.State.Running}}' bwlab-nim 2>/dev/null || echo false)"
      if [ "${running}" != "true" ]; then
        echo "nim-container-exited" >&2
        docker logs --tail 40 bwlab-nim 2>&1 | sed -E 's/nvapi-[A-Za-z0-9_-]+/[redacted]/g' >&2 || true
        exit 1
      fi
      sleep 15
    done
    if [ "${ready}" -ne 1 ]; then
      echo "nim-not-healthy" >&2
      docker logs --tail 40 bwlab-nim 2>&1 | sed -E 's/nvapi-[A-Za-z0-9_-]+/[redacted]/g' >&2 || true
      exit 1
    fi
    python3 - << 'PY'
import json
import urllib.error
import urllib.request

model = json.load(open("/tmp/nim-models.json"))["data"][0]["id"]
tool = {
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
}

def once(seed):
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": "Call the get_service_health tool for the api service.",
        }],
        "temperature": 0.2,
        "top_p": 0.95,
        # The workload streams up to 1024 tokens. A 128-token non-streaming
        # probe can finish inside the reasoning trace before any tool call.
        "max_tokens": 1024,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "tools": [tool],
    }
    if seed is not None:
        body["seed"] = seed
    request = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    calls = {}
    finish = None
    content_chars = 0
    reasoning_chars = 0
    try:
        response = urllib.request.urlopen(request, timeout=180)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"native-tool-calls-missing http={exc.code}") from None
    with response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            for choice in event.get("choices") or []:
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                content_chars += len(delta.get("content") or "")
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                reasoning_chars += len(reasoning)
                for tool_call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tool_call.get("index", 0), "")
                    function = tool_call.get("function") or {}
                    calls[tool_call.get("index", 0)] = slot + (function.get("name") or "")
    return calls, finish, content_chars, reasoning_chars

detail = "no-attempt"
for seed in (20260906, None):
    names, finish, content_chars, reasoning_chars = once(seed)
    if any(names.values()):
        print("nim-tools-ok")
        raise SystemExit(0)
    detail = (
        f"finish={finish} content_chars={content_chars} "
        f"reasoning_chars={reasoning_chars}"
    )
raise SystemExit(f"native-tool-calls-missing {detail}")
PY
    rm -f "${keyfile}"
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
    /opt/bwlab-venv/bin/python "${REPO}/scripts/aws_nim_bf16_cell.py" "${RESULTS}"
    ;;
  pack)
    tar -C /var/lib/bwlab -cf /var/lib/bwlab/results-export-nim.tar results
    echo "pack-complete"
    ;;
  *)
    echo "usage: aws_nim_bf16_remote.sh sync|serve|cell|pack" >&2
    exit 2
    ;;
esac
