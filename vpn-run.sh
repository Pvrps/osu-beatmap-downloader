#!/usr/bin/env bash
# vpn-run.sh — run a command inside an isolated network namespace that routes
# all traffic through an OpenVPN tunnel. Only the command's traffic uses the
# VPN; the rest of the system is unaffected.
#
# Usage: sudo vpn-run.sh <ovpn-config> <creds-file> -- <command...>
#
# Must be run as root (needs tun device, netns, iptables). The <command> is
# dropped back to the original user (SUDO_USER) inside the namespace.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: vpn-run.sh must be run as root (use sudo)" >&2
    exit 1
fi

# sudo strips most of PATH. Preserve the caller's PATH so nix-shell-provided
# tools (openvpn, ip, iptables) stay reachable inside this script.
if [ -n "${SUDO_PATH:-}" ]; then
    export PATH="$SUDO_PATH"
elif [ -n "${ORIGINAL_PATH:-}" ]; then
    export PATH="$ORIGINAL_PATH"
fi

if [ "$#" -lt 4 ] || [ "$3" != "--" ]; then
    echo "Usage: sudo vpn-run.sh <ovpn> <creds> -- <command...>" >&2
    exit 1
fi

OVPN="$1"
CREDS="$2"
shift 3
CMD=("$@")

NS="osu-vpn"
VETH_MAIN="v-osu0"
VETH_NS="v-osu1"
GATEWAY_IP="10.200.200.1"
NS_IP="10.200.200.2"
SUBNET="10.200.200.0/24"
TARGET_USER="${SUDO_USER:-root}"

# Find the default interface (for NAT to the internet).
DEFIF=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
if [ -z "$DEFIF" ]; then
    echo "ERROR: no default route — cannot determine uplink interface" >&2
    exit 1
fi

OVPN_PID=""
CREDS_OWNED_BY_US=false

cleanup() {
    echo "[vpn] tearing down..." >&2
    if [ -n "$OVPN_PID" ]; then
        kill "$OVPN_PID" 2>/dev/null || true
        wait "$OVPN_PID" 2>/dev/null || true
    fi
    ip netns del "$NS" 2>/dev/null || true
    # veth main side is auto-deleted when namespace is removed, but be safe
    ip link del "$VETH_MAIN" 2>/dev/null || true
    iptables -t nat -D POSTROUTING -s "$SUBNET" -o "$DEFIF" -j MASQUERADE 2>/dev/null || true
    rm -f "$CREDS" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[vpn] creating network namespace '$NS'..." >&2

# Clean up any stale namespace from a previous crashed run.
ip netns del "$NS" 2>/dev/null || true
ip link del "$VETH_MAIN" 2>/dev/null || true

# --- Network namespace + veth pair + NAT -------------------------------------

ip netns add "$NS"
ip link add "$VETH_MAIN" type veth peer name "$VETH_NS"
ip link set "$VETH_NS" netns "$NS"

# Main side acts as gateway
ip addr add "$GATEWAY_IP/24" dev "$VETH_MAIN"
ip link set "$VETH_MAIN" up

# Namespace side
ip -n "$NS" addr add "$NS_IP/24" dev "$VETH_NS"
ip -n "$NS" link set "$VETH_NS" up
ip -n "$NS" link set lo up
ip -n "$NS" route add default via "$GATEWAY_IP"

# Enable forwarding + masquerade namespace traffic out the default interface
sysctl -w net.ipv4.ip_forward=1 >/dev/null
iptables -t nat -A POSTROUTING -s "$SUBNET" -o "$DEFIF" -j MASQUERADE

# --- OpenVPN inside the namespace --------------------------------------------

echo "[vpn] starting OpenVPN ($OVPN)..." >&2

ip netns exec "$NS" openvpn \
    --config "$OVPN" \
    --auth-user-pass "$CREDS" \
    --verb 2 \
    --log /tmp/osu-openvpn.log &
OVPN_PID=$!

# Wait for tun0 to appear (VPN tunnel established)
echo "[vpn] waiting for tunnel..." >&2
TUNNED_UP=false
for i in $(seq 1 30); do
    if ! kill -0 "$OVPN_PID" 2>/dev/null; then
        echo "[vpn] ERROR: OpenVPN process died. Log:" >&2
        cat /tmp/osu-openvpn.log >&2 || true
        exit 1
    fi
    if ip -n "$NS" link show tun0 &>/dev/null; then
        TUNNED_UP=true
        break
    fi
    sleep 1
done

if [ "$TUNNED_UP" = false ]; then
    echo "[vpn] ERROR: tunnel did not come up within 30s. Log:" >&2
    cat /tmp/osu-openvpn.log >&2 || true
    exit 1
fi

echo "[vpn] tunnel is up — running command as $TARGET_USER" >&2

# --- Run the command as the original user inside the namespace ---------------

# Set OSU_VPN_ACTIVE so the child knows it's inside the VPN namespace and
# doesn't try to re-exec the VPN wrapper again. `sudo` resets env by default,
# so we pass the variables explicitly via the command environment.
ip netns exec "$NS" \
    env "OSU_VPN_ACTIVE=1" "HOME=/home/$TARGET_USER" "PATH=$PATH" \
    sudo -u "$TARGET_USER" --preserve-env=OSU_VPN_ACTIVE,HOME,PATH \
    "${CMD[@]}"
