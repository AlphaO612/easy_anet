#!/usr/bin/env bash
# Rendered and pinned by the release packager.
set -euo pipefail

update_panel() (
    set -euo pipefail
    umask 077
    local stage
    stage=$(mktemp -d)
    trap 'rm -r -- "$stage"' EXIT
    cd "$stage"
    echo "Скачиваю обновление панели для linux/amd64."
    curl --proto '=https' --proto-redir '=https' -fL --retry 2 -o panel.tar.gz @PANEL_URL@
    echo '@PANEL_SHA@  panel.tar.gz' | sha256sum --check --status || { echo 'Не совпала контрольная сумма обновления.' >&2; exit 1; }
    tar --no-same-owner -xzf panel.tar.gz
    python3 scripts/update-panel.py
)

exec 3</dev/tty || { echo "Откройте SSH с терминалом (ssh -tt)." >&2; exit 1; }
if (( EUID == 0 )); then
    owner=${1:-anet-vpn}
    uid=$(id -u -- "$owner") && [[ "$uid" != 0 ]] || { echo 'Укажите пользователя rootless Docker.' >&2; exit 1; }
    runuser -u "$owner" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" bash -c "$(declare -f update_panel); update_panel" <&3 3<&-
else
    update_panel <&3 3<&-
fi
