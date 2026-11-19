#!/usr/bin/env bash
# From the Mac: copies this checkout to raspy:~/llm-console and runs
# container/install.sh there (build, Quadlet unit, restart, health check).
# config.yaml goes too: the copy in this repository is the reference one.
#
# Usage: ./deploy.sh [host]   (default: raspy)
set -euo pipefail
host="${1:-raspy}"
here="$(cd "$(dirname "$0")" && pwd)"
version="$(git -C "$here" describe --always --dirty 2>/dev/null || echo unknown)"

rsync -az --delete \
    --exclude .git/ --exclude .venv/ --exclude .claude/ --exclude .pytest_cache/ \
    --exclude __pycache__/ --exclude .DS_Store \
    "$here/" "$host:llm-console/"

ssh "$host" "LLM_CONSOLE_VERSION='$version' ~/llm-console/container/install.sh"
