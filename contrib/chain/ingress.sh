#!/bin/sh
set -eu
umask 077
case "${INGRESS_PATH:?select chain or direct}" in
    chain)
        output=anet-uplink
        ip link show dev "$output" >/dev/null
        ip -4 route show table 177 | grep -Eq '^default .*dev anet-uplink( |$)'
        ;;
    direct)
        if ip link show dev anet-uplink >/dev/null 2>&1; then exit 2; fi
        output=$(ip -4 route show default | awk 'NR==1 {for (i=1;i<NF;i++) if ($i=="dev") print $(i+1)}')
        : "${output:?missing default route}"
        ;;
    *) exit 2 ;;
esac

rule() {
    family=$1 table=$2 chain=$3
    shift 3
    "$family" -w -t "$table" -C "$chain" "$@" 2>/dev/null ||
        "$family" -w -t "$table" -I "$chain" 1 "$@"
}

case "${1:-}" in
    wireguard)
        # The guard is in force before activation, even if table 177 disappears.
        rule iptables filter INPUT -i wg-profile -j DROP
        rule iptables filter FORWARD -i wg-profile ! -o "$output" -j DROP
        rule iptables filter FORWARD -i wg-profile -s 10.79.0.0/24 -o "$output" -j ACCEPT
        rule iptables filter FORWARD -o wg-profile -j DROP
        rule iptables filter FORWARD -i "$output" -o wg-profile -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        rule ip6tables filter INPUT -i wg-profile -j DROP
        rule ip6tables filter FORWARD -i wg-profile -j DROP
        rule iptables nat POSTROUTING -s 10.79.0.0/24 -o "$output" -j MASQUERADE
        if ip link show dev wg-profile >/dev/null 2>&1; then ip link del dev wg-profile; fi
        ip link add dev wg-profile type wireguard
        trap 'ip link del dev wg-profile' EXIT
        trap 'exit 0' TERM INT
        if [ "$INGRESS_PATH" = chain ]; then
            if ! ip -4 rule list | grep -Eq '^17177:.*iif wg-profile lookup 177$'; then
                ip -4 rule add priority 17177 iif wg-profile lookup 177
            fi
        fi
        wg setconf wg-profile /input/wg.conf
        ip addr add 10.79.0.1/24 dev wg-profile
        ip link set dev wg-profile mtu 1280 up
        while :; do sleep 3600 & wait $!; done
        ;;
    vless)
        # Match the dedicated UID for every outbound connection, including DNS.
        # The only unmarked replies allowed are on accepted TLS connections.
        rule ip6tables filter OUTPUT -m owner --uid-owner 17777 -j DROP
        rule iptables filter OUTPUT -m owner --uid-owner 17777 ! -o "$output" -j DROP
        rule iptables filter OUTPUT -m owner --uid-owner 17777 -p tcp --sport 8443 -m conntrack --ctdir REPLY -j ACCEPT
        if [ "$INGRESS_PATH" = chain ]; then
            # Select the uplink during the initial socket route lookup. Preserve
            # the return route on the TLS listener; OUTPUT checks conntrack too.
            if ! ip -4 rule show | grep -Eq '^10170:.*uidrange 17777-17777 .*sport 8443 .*lookup main'; then
                ip -4 rule add pref 10170 uidrange 17777-17777 ipproto tcp sport 8443 lookup main
            fi
            if ! ip -4 rule show | grep -Eq '^10171:.*uidrange 17777-17777 .*lookup 177'; then
                ip -4 rule add pref 10171 uidrange 17777-17777 lookup 177
            fi
            rule iptables mangle OUTPUT -m owner --uid-owner 17777 -m conntrack --ctdir ORIGINAL -j MARK --set-mark 177
            rule iptables nat POSTROUTING -m mark --mark 177 -o "$output" -j MASQUERADE
        fi
        for file in server.json tls.pem tls.key; do cp "/input/$file" "/run/$file"; done
        chmod 0600 /run/server.json
        chmod 0400 /run/tls.pem /run/tls.key
        chown 17777:17777 /run/server.json /run/tls.pem /run/tls.key
        exec setpriv --bounding-set=-all --reuid=17777 --regid=17777 --clear-groups --no-new-privs xray run -config /run/server.json
        ;;
    *) exit 2 ;;
esac
