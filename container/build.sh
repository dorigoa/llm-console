#!/usr/bin/env bash
# Builds the llm-console image from container/Containerfile, with `container`
# (macOS), podman or docker (Linux), as the host user. The build context is the
# repository root: the image holds app/, static/ and config.yaml as they are in
# this checkout, and its version (git describe) in LLM_CONSOLE_VERSION.
#
# Usage: container/build.sh [build options, e.g. --no-cache]
# Env:   RUNTIME (default: the first of container, podman, docker), IMAGE,
#        LLM_CONSOLE_VERSION (default: git describe; deploy.sh passes it, raspy has no .git)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"

RUNTIME="${RUNTIME:-$(command -v container || command -v podman || command -v docker || true)}"
IMAGE="${IMAGE:-llm-console:latest}"

if [[ -z "$RUNTIME" ]]; then
    echo "build.sh: needs container (macOS), podman or docker" >&2
    exit 1
fi

version="${LLM_CONSOLE_VERSION:-$(git -C "$root" describe --always --dirty 2>/dev/null || echo unknown)}"

"$RUNTIME" build -f "$here/Containerfile" -t "$IMAGE" \
    --build-arg USER_NAME="$(id -un)" \
    --build-arg USER_UID="$(id -u)" \
    --build-arg USER_GID="$(id -g)" \
    --build-arg LLM_CONSOLE_VERSION="$version" \
    "$@" "$root"
echo "build.sh: $IMAGE built from llm-console $version"
