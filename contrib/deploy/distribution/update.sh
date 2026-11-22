#!/usr/bin/env bash
# Filled by package.py for one GitHub Release and one compatible image manifest.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }

verify_update_file() {
    local file=$1 expected=$2
    [[ -f "$file" && -r "$file" ]] || fail "Файл $file отсутствует или недоступен."
    local actual
    actual=$(sha256sum < "$file")
    [[ "${actual%% *}" == "$expected" ]] || fail "Не совпадает SHA-256 файла $file. Проверьте версию комплекта и повторите запуск."
}

apply_update() {
    local url=@UPDATE_URL@
    local archive_sum=@UPDATE_SHA256@
    local manifest_sum=@MANIFEST_SHA256@
    local source_sum=@SOURCE_SHA256@
    local installer_sum=@INSTALLER_SHA256@
    local old dest input
    (( $# <= 1 )) || fail 'Запустите скрипт из каталога прежней поставки. Единственный необязательный аргумент — путь к скачанному anet-easy-update.tar.gz.'
    old=$(pwd -P)
    [[ -f "$old/images.tar" && -f "$old/manifest.json" ]] ||
        fail 'Перейдите командой cd в каталог прежней поставки, где рядом лежат images.tar и manifest.json, затем повторите запуск.'
    if [[ $(sha256sum < "$old/manifest.json" | cut -d' ' -f1) != "$manifest_sum" ]]; then
        printf 'Образы в этом релизе обновились. Выполните на первой VPS для загрузки полной версии со своим сохранённым планом:\n  curl -fL --retry 2 %s/install-release.sh | bash\n' "${url%/*}" >&2
        return 1
    fi
    [[ -r "$old/images.tar" ]] || fail 'Проверьте доступ на чтение images.tar в прежнем комплекте.'
    { exec 3<>/dev/tty; } 2>/dev/null || fail 'Откройте обычный SSH-сеанс с терминалом и повторите команду.'
    umask 077
    dest="$old/anet-update-${source_sum:0:16}-${manifest_sum:0:12}"
    exec 9<"$old"
    flock -x 9
    if [[ ! -e "$dest" && ! -L "$dest" ]]; then
        stage=$(mktemp -d "$old/.anet-update.XXXXXXXX") || fail 'Для обновления нужна запись в каталог прежнего комплекта.'
        trap 'rm -rf -- "$stage"' EXIT
        trap 'exit 130' INT HUP TERM
        input=${1:-"$stage/update.tar.gz"}
        if (( $# == 0 )); then
            printf 'Скачиваю обновление установщика. Образы остаются на VPS.\n' >&2
            curl --proto '=https' --proto-redir '=https' --fail --show-error --location --retry 3 \
                --progress-bar --output "$input" -- "$url" ||
                fail 'Не удалось скачать обновление. Проверьте соединение с GitHub Release и повторите команду.'
        fi
        verify_update_file "$input" "$archive_sum"
        mkdir "$stage/installer"
        tar -xzf "$input" --no-same-owner -C "$stage/installer" -- install.sh source.tar.gz
        verify_update_file "$stage/installer/source.tar.gz" "$source_sum"
        verify_update_file "$stage/installer/install.sh" "$installer_sum"
        chmod 700 "$stage/installer/install.sh"
        ln -s ../images.tar "$stage/installer/images.tar"
        ln -s ../manifest.json "$stage/installer/manifest.json"
        mv -T -- "$stage/installer" "$dest"
        rm -rf -- "$stage"
        trap - EXIT INT HUP TERM
    else
        [[ -d "$dest" && ! -L "$dest" ]] || fail "Путь обновления $dest занят. Уточните содержимое каталога."
        verify_update_file "$dest/install.sh" "$installer_sum"
        verify_update_file "$dest/source.tar.gz" "$source_sum"
        [[ "$dest/images.tar" -ef "$old/images.tar" && "$dest/manifest.json" -ef "$old/manifest.json" ]] ||
            fail 'Ссылки в каталоге обновления не ведут к файлам прежнего комплекта.'
    fi
    exec 9<&-
    printf 'Новый установщик: %s/install.sh\n' "$dest" >&2
    exec bash "$dest/install.sh" <&3 3>&-
}

apply_update "$@"
