#!/usr/bin/env bash
# On raspy (Linux, rootless podman): builds the image from this checkout, installs
# the Quadlet unit and the LAN forwarder unit, and (re)starts the service, which
# from then on starts by itself at every boot, together with the forwarder.
# Run it again to update; ../deploy.sh runs it from the Mac.
#
# The units read ~/llm-console/config.yaml and ~/llm-console/container/lan.env:
# the checkout must be ~/llm-console.
set -euo pipefail
cd "$(dirname "$0")"
unit=llm-console.container
service=llm-console.service
lan_unit=llm-console-lan.service
quadlet_dir="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

fail() { echo "install.sh: $*" >&2; exit 1; }

# Exit 0 if something listens on exactly addr:port.
listening() { ss -ltnH "sport = :$2" | awk '{print $4}' | grep -qxF "$1:$2"; }

command -v podman >/dev/null || fail "needs podman (the unit is a Quadlet: podman-systemd.unit(5))"
command -v socat >/dev/null || fail "needs socat (for $lan_unit): sudo apt install socat"

# Published address, e.g. PublishPort=127.0.0.1:80:8080 -> 127.0.0.1 and 80.
publish=$(awk -F= '$1 == "PublishPort" { print $2; exit }' "$unit")
addr=${publish%%:*}
rest=${publish#*:}
port=${rest%%:*}

# LAN_ADDRESS, LAN_PORT, APP_ADDRESS.
# shellcheck source=lan.env
source ./lan.env
[[ $APP_ADDRESS == "$addr:$port" ]] || fail "lan.env: APP_ADDRESS=$APP_ADDRESS, but $unit publishes $addr:$port"
ip -4 -o addr show | grep -qF " inet $LAN_ADDRESS/" \
    || fail "lan.env: raspy has no address $LAN_ADDRESS (DHCP changed it?): $(ip -4 -o addr show | awk '{print $2, $4}' | tr '\n' ' ')"

# Rootless podman and socat can bind a port below 1024 only if the kernel allows it.
unprivileged=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)
for p in "$port" "$LAN_PORT"; do
    if (( p < unprivileged )); then
        fail "port $p needs net.ipv4.ip_unprivileged_port_start <= $p (now $unprivileged), see PLAN.md Fase 0"
    fi
done

# The ports must be free, unless the services already hold them.
if ! systemctl --user is-active --quiet "$service" && listening "$addr" "$port"; then
    fail "$addr:$port is already in use: free it first"
fi
if ! systemctl --user is-active --quiet "$lan_unit" && listening "$LAN_ADDRESS" "$LAN_PORT"; then
    fail "$LAN_ADDRESS:$LAN_PORT is already in use: free it first"
fi

# What the units mount or read must exist, or they do not start.
checkout="$(cd .. && pwd -P)"
[[ "$checkout" == "$(cd ~/llm-console 2>/dev/null && pwd -P)" ]] \
    || fail "the checkout is $checkout, but the units read ~/llm-console/config.yaml and container/lan.env"
for f in ~/.ssh/id_ecdsa ~/.ssh/id_ecdsa.pub ~/.ssh/known_hosts "$checkout/config.yaml"; do
    [[ -f $f ]] || fail "missing $f (mounted by $unit)"
done

# Quadlet runs the image from podman's store.
RUNTIME=podman ./build.sh

mkdir -p "$quadlet_dir" "$unit_dir"
install -m 644 "$unit" "$quadlet_dir/$unit"
install -m 644 "$lan_unit" "$unit_dir/$lan_unit"
systemctl --user daemon-reload
# Starts the web app, or restarts it with the image just built; Wants= starts the
# forwarder with it. Restarting the forwarder too picks up a changed lan.env.
systemctl --user restart "$service"
systemctl --user restart "$lan_unit"

# Without lingering the services start at the first login instead of at boot.
if [[ $(loginctl show-user "$(id -un)" -p Linger --value) != yes ]]; then
    echo "install.sh: to start at boot without logging in, run: loginctl enable-linger" >&2
fi

up() { curl -fs -o /dev/null --max-time 5 "http://$1/healthz"; }
for _ in {1..20}; do
    if up "$addr:$port"; then
        version=$(podman exec "${service%.service}" cat /app/VERSION 2>/dev/null || true)
        echo "llm-console $version is up: http://$addr:$port"
        for _ in {1..10}; do
            if up "$LAN_ADDRESS:$LAN_PORT"; then
                echo "llm-console is up on the LAN: http://$LAN_ADDRESS:$LAN_PORT"
                exit 0
            fi
            sleep 1
        done
        fail "not answering on the LAN at $LAN_ADDRESS:$LAN_PORT, see: journalctl --user-unit ${lan_unit%.service}"
    fi
    sleep 1
done
fail "llm-console is not answering on $addr:$port, see: journalctl --user-unit ${service%.service}"
