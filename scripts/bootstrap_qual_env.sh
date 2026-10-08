#!/usr/bin/env bash
# Create (or re-verify) the pinned, isolated qualification Python environment
# required by `blackwell-cloud qualify-agent` and `canary-agent` (decision
# D-0031). `analyze-qualification` is read-only and does not use this environment.
#
# - Never modifies system Python. Everything lands in one virtual environment
#   whose default location is outside this repository.
# - Installs this package and the exact pins from constraints.txt, which
#   include the JSON Schema format extras (rfc3339-validator) whose absence
#   aborted qual-p1-b before any task ran.
# - Verifies that the `date` and `date-time` format checkers are registered
#   and writes a content-free marker recording the exact installed versions.
# - Idempotent: re-running on an existing environment re-pins and re-verifies.
# - Uses only the package index already used by the credential-free developer
#   workflow (`pip install -e ".[dev]" -c constraints.txt`). No model, result,
#   or credential is downloaded or stored.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUAL_ENV_DIR="${BWLAB_QUAL_ENV_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/blackwell-lab/qual-env}"
QUAL_PYTHON="${BWLAB_QUAL_PYTHON:-python3.12}"

if ! command -v "$QUAL_PYTHON" >/dev/null 2>&1; then
  echo "BLOCKED: interpreter '$QUAL_PYTHON' not found; set BWLAB_QUAL_PYTHON" >&2
  exit 1
fi

# Canonicalize before creating anything. A raw string compare misses relative
# paths and symlink parents that resolve inside the repository.
QUAL_ENV_DIR="$(
  BWLAB_QUAL_ENV_DIR="$QUAL_ENV_DIR" BWLAB_REPO_ROOT="$REPO_ROOT" "$QUAL_PYTHON" - <<'PY'
import os
import sys
from pathlib import Path


def canonicalize(raw: str, depth: int = 0) -> Path:
    """Resolve existing symlink components. Do not create missing ones."""
    if depth > 40:
        print(
            "BLOCKED: BWLAB_QUAL_ENV_DIR must resolve outside the repository",
            file=sys.stderr,
        )
        sys.exit(1)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    root = Path(path.anchor)
    pending = list(path.parts[1:])
    current = root
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if current != root:
                current = current.parent
            continue
        candidate = current / part
        if candidate.is_symlink():
            target = Path(os.readlink(candidate))
            if not target.is_absolute():
                target = current / target
            resolved = canonicalize(str(target), depth + 1)
            if pending:
                return canonicalize(str(resolved.joinpath(*pending)), depth + 1)
            return resolved
        if candidate.exists():
            current = candidate
            continue
        current = candidate
        for rest in pending:
            if rest in ("", "."):
                continue
            if rest == "..":
                if current != root:
                    current = current.parent
            else:
                current = current / rest
        return current
    return current


repo = Path(os.environ["BWLAB_REPO_ROOT"]).resolve()
qual = canonicalize(os.environ["BWLAB_QUAL_ENV_DIR"])
try:
    qual.relative_to(repo)
except ValueError:
    inside = False
else:
    inside = True
if inside:
    print(
        "BLOCKED: BWLAB_QUAL_ENV_DIR must resolve outside the repository",
        file=sys.stderr,
    )
    sys.exit(1)
print(qual)
PY
)"

if [ ! -x "$QUAL_ENV_DIR/bin/python" ]; then
  mkdir -p "$(dirname "$QUAL_ENV_DIR")"
  "$QUAL_PYTHON" -m venv "$QUAL_ENV_DIR"
fi

VENV_PYTHON="$QUAL_ENV_DIR/bin/python"
"$VENV_PYTHON" -m pip install --quiet --upgrade "pip<26" >/dev/null
"$VENV_PYTHON" -m pip install --quiet -e "$REPO_ROOT" -c "$REPO_ROOT/constraints.txt"

"$VENV_PYTHON" - <<'PY'
import json
import sys

from blackwell_lab.cloud import qual_env

marker = qual_env.marker_path()
qual_env.write_marker(marker)
report = qual_env.inspect_environment()
print(json.dumps({k: report[k] for k in ("python_version", "isolated", "marker_valid", "format_checkers", "problems", "ok")}, indent=2))
if not report["ok"]:
    print("BLOCKED: qualification environment incomplete", file=sys.stderr)
    sys.exit(1)
PY

echo "qualification environment ready; invoke qualification commands as:"
echo "  $QUAL_ENV_DIR/bin/blackwell-cloud <command> ..."
