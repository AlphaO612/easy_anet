#!/usr/bin/env bash
# package.py writes a pinned launcher for the chosen distribution method.
set -euo pipefail

message() { printf '%s\n' "$*" >&2; }
fail() { message "$@"; exit 1; }

install_release() {
    local base_url=@BASE_URL@
    local source_sum=@SOURCE_SHA256@
    local manifest_sum=@MANIFEST_SHA256@
    local images_sum=@IMAGES_SHA256@
    local unpack_bytes=@UNPACK_BYTES@
    local download_bytes=@DOWNLOAD_BYTES@
    local releases="${XDG_DATA_HOME:-$HOME/.local/share}/anet/releases"
    local release="$releases/$source_sum-$manifest_sum"
    local bundle_dir="$release/bundle" source_archive required=$unpack_bytes

    { exec 3<>/dev/tty; } 2>/dev/null || fail 'Мастеру нужен терминал. Войдите на VPS обычной командой ssh (при необходимости с ключом -t) и повторите запуск в открывшемся сеансе.'
    case "$*" in
        ""|--plain|--access|"--access --plain"|"--plain --access") ;;
        *) fail 'Используйте bash ./install.sh, --plain для текстового меню или --access для доступа к панели.' ;;
    esac
    trap 'message "Подготовка прервана. Вернитесь в SSH-сеанс и повторите исходную команду."; exit 130' INT HUP TERM
    message '1/3  Подготовка каталога установки'
    command -v python3 >/dev/null || fail 'Требуется Python 3. На Debian или Ubuntu установите пакет python3 и повторите запуск.'
    [[ "$releases" == /* ]] || fail 'В XDG_DATA_HOME укажите полный путь к своему каталогу на диске и повторите запуск.'
    umask 077
    mkdir -p -- "$releases" || fail "Проверьте права записи и свободное место в $releases и повторите запуск."
    case "$base_url" in
        https://*) required=$((unpack_bytes + download_bytes));;
        '')
            bundle_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
            release="$release-local"
            ;;
        *) fail 'Запустите install.sh или HTTPS-команду, полученную от владельца поставки.';;
    esac
    exec 9>"$release.lock"
    if ! flock -n -x 9; then
        message 'Другой запуск готовит этот выпуск. Ожидаю завершения.'
        flock -x 9 || fail 'Закройте параллельный запуск и повторите команду.'
    fi
    if [[ ! -d "$release" ]]; then check_space "$releases" "$required"; fi

    message '2/3  Файлы поставки'
    if [[ -z "$base_url" ]]; then
        verify "$bundle_dir" manifest.json "$manifest_sum"
        verify "$bundle_dir" source.tar.gz "$source_sum"
        verify "$bundle_dir" images.tar "$images_sum"
    fi
    if [[ ! -d "$release" ]]; then
        stage=$(mktemp -d "$releases/.download.XXXXXXXX")
        trap 'rm -rf -- "$stage"' EXIT
        mkdir "$stage/source"
        if [[ -n "$base_url" ]]; then
            mkdir "$stage/bundle"
            fetch "$base_url" manifest.json "$stage/bundle" "$manifest_sum"
            fetch "$base_url" source.tar.gz "$stage" "$source_sum"
            fetch "$base_url" images.tar "$stage/bundle" "$images_sum"
            source_archive="$stage/source.tar.gz"
        else
            source_archive="$bundle_dir/source.tar.gz"
        fi
        message '3/3  Распаковка установщика'
        stream_file unpack "$source_archive" | tar --no-same-owner -xz -C "$stage/source" ||
            fail "Распаковка не завершена. Проверьте свободное место (df -h) и права записи в $releases, затем повторите запуск."
        test -x "$stage/source/contrib/deploy/install.sh" ||
            fail 'В архиве отсутствует исполняемый установщик. Получите полный выпуск у владельца поставки и повторите запуск.'
        if [[ -n "$base_url" ]]; then rm -- "$source_archive"; fi
        mv -T -- "$stage" "$release"
        trap - EXIT
    else
        message '3/3  Использую ранее распакованный установщик этого выпуска.'
    fi
    exec 9>&-
    export ANET_BUNDLE_DIR="$bundle_dir"
    cd "$release/source"
    message 'Открываю мастер установки.'
    exec ./contrib/deploy/install.sh "$@" <&3 3>&-
}

check_space() {
    local dir=$1 bytes=$2 free_kib need_kib
    free_kib=$(LC_ALL=C df -Pk -- "$dir" | awk 'END {print $4}') || fail "Проверьте доступ к диску командой df -h и повторите запуск."
    [[ "$free_kib" =~ ^[0-9]+$ ]] || fail "Не удалось определить свободное место в $dir. Проверьте файловую систему командой df -h."
    need_kib=$(( (bytes + 1023) / 1024 ))
    if (( free_kib < need_kib )); then
        fail "Недостаточно места в $dir. Для загрузчика нужно $(( (need_kib + 1023) / 1024 )) МиБ, доступно $(( free_kib / 1024 )) МиБ. Найдите свободный диск командой df -h, задайте XDG_DATA_HOME полным путём к своему каталогу на этом диске и повторите запуск."
    fi
}

# Stream the archive once per operation. The status counts real input bytes.
stream_file() {
    python3 - "$@" <<'PY'
import hashlib
import os
from pathlib import Path
import sys
import time

mode, path, *expected = sys.argv[1:]
name = Path(path).name
label = "SHA-256" if mode == "verify" else "Распаковка"
terminal = sys.stderr.isatty()
digest = hashlib.sha256()
try:
    with open(path, "rb") as source:
        total = os.fstat(source.fileno()).st_size
        done = 0
        last = time.monotonic()
        def status(end):
            percent = 100 * done // total if total else 100
            amount = (f"{done / 2**20:.1f} / {total / 2**20:.1f} МиБ" if total >= 2**20
                      else f"{done} / {total} байт")
            print(f"  {label} {name}   {percent:3}%   {amount}",
                  file=sys.stderr, end=end, flush=True)
        if total >= 2**20:
            status("\r" if terminal else "\n")
        while chunk := source.read(1024 * 1024):
            if mode == "verify":
                digest.update(chunk)
            else:
                sys.stdout.buffer.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last >= (0.4 if terminal else 5):
                status("\r" if terminal else "\n")
                last = now
        status("\n")
    if mode == "verify" and digest.hexdigest() != expected[0]:
        print(f"SHA-256 файла {name} отличается от указанного в поставке.", file=sys.stderr)
        sys.exit(1)
except OSError as exc:
    sys.exit(f"Ошибка чтения или передачи {name}: {exc.strerror}")
PY
}

verify() {
    local dir=$1 name=$2 sum=$3
    [[ -f "$dir/$name" && -r "$dir/$name" ]] ||
        fail "Файл $name отсутствует или недоступен для чтения. Передайте этот файл из того же выпуска в $dir и повторите запуск."
    if ! stream_file verify "$dir/$name" "$sum"; then
        if [[ -z "$base_url" ]]; then
            fail "Передайте $name из исходного комплекта того же выпуска заново и повторите запуск install.sh."
        fi
        fail "Повторите команду установки. Если ошибка проверки повторится, запросите у владельца проверенную копию $name для этого выпуска."
    fi
}

fetch() {
    local url=$1 name=$2 dir=$3 sum=$4 status
    message "Скачиваю $name"
    if ! status=$(curl --proto '=https' --proto-redir '=https' --netrc-optional --fail --show-error \
        --progress-bar --location --connect-timeout 15 --speed-limit 1 --speed-time 60 --retry 3 \
        --output "$dir/$name" --write-out '%{http_code}' -- "$url/$name"); then
        case "$status" in
            401|403) fail "Доступ к $name отклонён. Проверьте реквизиты HTTPS-хоста в ~/.netrc и повторите команду.";;
            404) fail "Файл $name отсутствует на сервере. Уточните адрес выпуска у владельца и повторите команду.";;
            *) fail "Загрузка $name прервалась. Проверьте соединение с сервером поставки и свободное место (df -h), затем повторите команду установки.";;
        esac
    fi
    verify "$dir" "$name" "$sum"
}

# Keep the entry point last so a truncated pipe cannot start an installation halfway through a function.
install_release "$@"
