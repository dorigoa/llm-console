#!/bin/sh
# Puts the ssh key for the remote hosts into ~/.ssh, then runs the container's
# command (llm-console by default). Adapted from ai-console.
#
# llm-console.container mounts the key read-only in /run/host-ssh/nodes and
# raspy's known_hosts in /run/host-ssh/known_hosts (config.yaml points ssh at
# both). The key is copied so that in ~/.ssh it belongs to the user and has the
# permissions ssh requires.
set -eu

src=/run/host-ssh/nodes
dst="$HOME/.ssh"

mkdir -p "$dst"
chmod 700 "$dst"

keys=""
for f in "$src"/*; do
    [ -f "$f" ] || continue
    name=${f##*/}
    case "$name" in
        *.pub) install -m 644 "$f" "$dst/$name" ;;
        *)     install -m 600 "$f" "$dst/$name"; keys="$keys $name" ;;
    esac
done

if [ -z "$keys" ]; then
    echo "entrypoint: no keys in $src: llm-console cannot reach the hosts (fine with LLM_CONSOLE_DRY_RUN=1)" >&2
fi
if [ ! -f /run/host-ssh/known_hosts ]; then
    echo "entrypoint: no /run/host-ssh/known_hosts: every ssh will fail the host key check" >&2
fi

exec "$@"
