#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
    [ "${1:-}" = --wizard-user ] || { echo 'Run as the deployment user with sudo access' >&2; exit 2; }
    deploy_user=anet-vpn
    if ! id "$deploy_user" >/dev/null 2>&1; then useradd -m -s /bin/bash "$deploy_user"; fi
    [ "$(id -u "$deploy_user")" -ge 1000 ] || { echo 'Expected unprivileged anet-vpn account' >&2; exit 2; }
    privileged() { "$@"; }
else
    deploy_user=$(id -un)
    privileged() { sudo "$@"; }
fi
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo "Automatic Docker bootstrap supports Ubuntu and Debian" >&2; exit 2;; esac
if ! command -v dockerd-rootless-setuptool.sh >/dev/null || ! docker compose version >/dev/null 2>&1; then
    existing_daemon=0
    if systemctl cat docker.service >/dev/null 2>&1; then existing_daemon=1; fi
    privileged apt-get update
    privileged apt-get install -y ca-certificates curl gnupg uidmap dbus-user-session slirp4netns
    keyfile=$(mktemp)
    trap 'rm -f "$keyfile"' EXIT
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$keyfile"
    actual=$(gpg --show-keys --with-colons "$keyfile" | awk -F: '$1=="fpr"{print $10; exit}')
    [ "$actual" = 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ] || { echo 'Unexpected Docker signing key' >&2; exit 1; }
    privileged install -d -m 0755 /etc/apt/keyrings
    privileged install -m 0644 "$keyfile" /etc/apt/keyrings/anet-docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/anet-docker.asc] https://download.docker.com/linux/%s %s stable\n' \
         "$(dpkg --print-architecture)" "$ID" "$VERSION_CODENAME" | privileged tee /etc/apt/sources.list.d/anet-docker.list >/dev/null
    privileged apt-get update
    version=${DOCKER_DEB_VERSION:-$(apt-cache policy docker-ce | awk '/Candidate:/{print $2}')}
    privileged apt-get install -y "docker-ce=$version" "docker-ce-cli=$version" "docker-ce-rootless-extras=$version" docker-buildx-plugin docker-compose-plugin
    if [ "$existing_daemon" = 0 ]; then privileged systemctl disable --now docker.service docker.socket; fi
fi
privileged loginctl enable-linger "$deploy_user"
if [ "$(id -u)" = 0 ]; then
    deploy_uid=$(id -u "$deploy_user")
    systemctl start "user@$deploy_uid.service"
    exec runuser -u "$deploy_user" -- env XDG_RUNTIME_DIR="/run/user/$deploy_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$deploy_uid/bus" sh -se <<'ROOTLESS'
if ! systemctl --user cat docker.service >/dev/null 2>&1; then dockerd-rootless-setuptool.sh install --force; fi
systemctl --user enable --now docker.service
docker context use rootless
ROOTLESS
fi
if ! systemctl --user cat docker.service >/dev/null 2>&1; then
    dockerd-rootless-setuptool.sh install --force
fi
systemctl --user enable --now docker.service
docker context use rootless
