#!/bin/sh
set -eu
if [ "${1:-}" = hold ]; then exec sleep infinity; fi
: "${IF_NAME:?}" "${TUN_ADDRESS:?}" "${TUN_SUBNET:?}" "${TUN_MTU:?}"
TUN_UID=${TUN_UID:-65532}
case "$IF_NAME" in -*|*/*|.*) exit 2;; esac
# Docker restart policies can restart sibling processes before Compose health
# dependencies run. Close forwarding before the inbound TUN can accept traffic.
if [ "${1:-}" = prepare-chain-and-hold ]; then
    iptables -P FORWARD DROP
fi
if ! ip link show dev "$IF_NAME" >/dev/null 2>&1; then
    ip tuntap add dev "$IF_NAME" mode tun user "$TUN_UID"
fi
test "$(cat "/sys/class/net/$IF_NAME/owner")" = "$TUN_UID"
ip addr replace "$TUN_ADDRESS" dev "$IF_NAME"
ip link set dev "$IF_NAME" mtu "$TUN_MTU" up
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1
if [ "${1:-}" = prepare-chain-and-hold ]; then
    # This namespace is dedicated to one entry profile. A disconnected
    # persistent TUN drops packets; the terminal route also covers deletion.
    ensure_rule() {
        table=$1
        shift
        iptables -t "$table" -C "$@" 2>/dev/null || iptables -t "$table" -I "$@"
    }
    ensure_rule filter INPUT -i "$IF_NAME" -j DROP
    ensure_rule filter FORWARD -i "$IF_NAME" -s "$TUN_SUBNET" -o anet-uplink -j ACCEPT
    ensure_rule filter FORWARD -i anet-uplink -o "$IF_NAME" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ensure_rule filter OUTPUT -m mark --mark 177 ! -o anet-uplink -j DROP
    ensure_rule nat POSTROUTING -s "$TUN_SUBNET" -o anet-uplink -j MASQUERADE
    ip -4 route replace unreachable default metric 32767 table 177
    if ! ip link show dev anet-uplink >/dev/null 2>&1; then
        ip tuntap add dev anet-uplink mode tun user 0
    fi
    ip link set dev anet-uplink up
    ip -4 route replace default dev anet-uplink metric 10 table 177
    if ! ip -4 rule show | grep -q 'iif '"$IF_NAME"' lookup 177'; then
        ip -4 rule add pref 10177 iif "$IF_NAME" lookup 177
    fi
    if ! ip -4 rule show | grep -q 'fwmark 0xb1 lookup 177'; then
        ip -4 rule add pref 10178 fwmark 177 lookup 177
    fi
else
    egress=$(ip -4 route show default | awk 'NR==1 {for (i=1;i<NF;i++) if ($i=="dev") print $(i+1)}')
    : "${egress:?missing IPv4 default route}"
    if ! iptables -t nat -C POSTROUTING -s "$TUN_SUBNET" -o "$egress" -j MASQUERADE 2>/dev/null; then
        iptables -t nat -A POSTROUTING -s "$TUN_SUBNET" -o "$egress" -j MASQUERADE
    fi
fi
if [ "${1:-}" = prepare-and-hold ] || [ "${1:-}" = prepare-chain-and-hold ]; then
    exec setpriv --bounding-set=-all --reuid=65532 --regid=65532 --clear-groups --no-new-privs sleep infinity
fi
