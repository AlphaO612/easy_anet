#!/usr/bin/env bash
# Rendered by the publisher after the full ZIP has been verified.
set -euo pipefail
umask 077
launch() (
    local base="${XDG_DATA_HOME:-$HOME/.local/share}/anet-easy/releases" dest stage
    dest="$base/@TAG@"
    mkdir -p "$base"
    exec 9<"$base"
    flock -x 9
    if [[ ! -d "$dest" ]]; then
        stage=$(mktemp -d "$base/.download.XXXXXXXX")
        trap 'rm -r -- "$stage"' EXIT
        echo 'Скачиваю полный комплект на первую VPS. Сохранённый план будет доступен в мастере.'
        curl --proto '=https' --proto-redir '=https' -fL --retry 2 -o "$stage/release.zip" @ZIP_URL@
        echo "@ZIP_SHA@  $stage/release.zip" | sha256sum --check --status || { echo 'Ошибка проверки ZIP.' >&2; exit 1; }
        python3 -m zipfile -e "$stage/release.zip" "$stage"
        mv -- "$stage/anet-easy" "$dest"
        rm -r -- "$stage"
        trap - EXIT
    fi
    exec 9<&-
    echo "Каталог релиза: $dest"
    exec bash "$dest/install.sh" </dev/tty
)
launch
