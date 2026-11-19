#!/usr/bin/env bash
# On raspy (Linux, rootless podman): builds the image from this checkout, installs
# the Quadlet unit and (re)starts the service, which from then on starts by itself
# at every boot. Run it again to update; ../deploy.sh runs it from the Mac.
#
# The unit mounts ~/llm-console/config.yaml: the checkout must be ~/llm-console.
set -euo pipefail
cd "$(dirname "$0")"
unit=llm-console.container
service=llm-console.service
dir="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"

fail() { echo "install.sh: $*" >&2; exit 1; }

command -v podman >/dev/null || fail "needs podman (the unit is a Quadlet: podman-systemd.unit(5))"

# Published address, e.g. PublishPort=127.0.0.1:80:8080 -> 127.0.0.1 and 80.
publish=$(awk -F= '$1 == "PublishPort" { print $2; exit }' "$unit")
addr=${publish%%:*}
rest=${publish#*:}
port=${rest%%:*}

# Rootless podman can bind a port below 1024 only if the kernel allows it.
unprivileged=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)
if (( port < unprivileged )); then
    fail "port $port needs net.ipv4.ip_unprivileged_port_start <= $port (now $unprivileged), see PLAN.md Fase 0"
fi

# The port must be free, unless the service already holds it.
if ! systemctl --user is-active --quiet "$service" && [[ -n $(ss -ltnH "sport = :$port") ]]; then
    fail "port $port is already in use: free it first"
fi

# What the unit mounts must exist, or the container does not start.
checkout="$(cd .. && pwd -P)"
[[ "$checkout" == "$(cd ~/llm-console 2>/dev/null && pwd -P)" ]] \
    || fail "the checkout is $checkout, but the unit mounts ~/llm-console/config.yaml"
for f in ~/.ssh/id_ecdsa ~/.ssh/id_ecdsa.pub ~/.ssh/known_hosts "$checkout/config.yaml"; do
    [[ -f $f ]] || fail "missing $f (mounted by $unit)"
done

# Quadlet runs the image from podman's store.
RUNTIME=podman ./build.sh

mkdir -p "$dir"
install -m 644 "$unit" "$dir/$unit"
systemctl --user daemon-reload
systemctl --user restart "$service"   # starts it, or restarts it with the image just built

# Without lingering the service starts at the first login instead of at boot.
if [[ $(loginctl show-user "$(id -un)" -p Linger --value) != yes ]]; then
    echo "install.sh: to start at boot without logging in, run: loginctl enable-linger" >&2
fi

for _ in {1..20}; do
    if curl -fs -o /dev/null "http://$addr:$port/healthz"; then
        echo "llm-console $(podman exec "${service%.service}" printenv LLM_CONSOLE_VERSION 2>/dev/null) is up: http://$addr:$port"
        exit 0
    fi
    sleep 1
done
fail "llm-console is not answering on $addr:$port, see: journalctl --user-unit ${service%.service}"
