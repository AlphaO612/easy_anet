#!/bin/sh
set -eu
# Keep the dedicated namespace closed before either the server or its clients start.
iptables -P FORWARD DROP
# Keep INPUT and FORWARD guarded throughout a repeated initialization.
nft add table ip anet_flow
nft -f - <<'NFT'
delete table ip anet_flow
table ip anet_flow {
  chain forward { type filter hook forward priority filter; policy drop; }
  chain dns_output { type filter hook output priority filter; policy accept; meta skgid 53153 drop; }
  chain input { type filter hook input priority filter; policy accept; iifname "anet-server" drop; }
}
NFT
/prepare-tun.sh prepare
# The dedicated nft chain now controls every forwarded packet in the namespace.
iptables -P FORWARD ACCEPT
touch /run/anet-flow/ready
exec setpriv --bounding-set=-all --reuid=65532 --regid=65532 --clear-groups --no-new-privs sleep infinity
