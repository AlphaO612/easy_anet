#!/usr/bin/env bash
# Install one panel-managed standalone server. The invitation arrives on stdin/tty only.
set -euo pipefail
umask 077
fail() { printf '%s\n' "$*" >&2; exit 1; }
case "${1:-}" in ''|--stdin) ;; *) fail 'Использование: bash join-node.sh [--stdin]' ;; esac
[[ $(uname -sm) = 'Linux x86_64' && -c /dev/net/tun ]] || fail 'Нужна Linux VPS на amd64 с /dev/net/tun.'
for cmd in python3 curl flock; do command -v "$cmd" >/dev/null || fail "Установите $cmd и повторите запуск."; done
# Keep the same rootless daemon and node identity when switching between SSH accounts or UI modes.
if [[ $(id -u) != 0 ]]; then
    if [[ ${1:-} = --stdin ]]; then exec sudo -n -H bash "$0" "$@"; fi
    echo 'Введите пароль sudo для подготовки VPS. Сервер будет работать от пользователя anet-vpn.'
    exec sudo -H bash "$0" "$@"
fi
stage=$(mktemp -d /tmp/anet-join.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
if [[ ${1:-} = --stdin ]]; then
    IFS= read -r invitation || fail 'Ожидается одна строка приглашения.'
fi
tag=anet-easy-2026.11.26.1
base="https://github.com/AlphaO612/easy_anet/releases/download/$tag"
zip=anet-easy-linux-amd64.zip
cache="${XDG_CACHE_HOME:-$HOME/.cache}/anet-join/$tag"
mkdir -p "$cache"
exec 9>"$cache/.lock"
flock 9
download() { curl --proto '=https' --proto-redir '=https' -fLsS --retry 2 -o "$2" -- "$base/$1"; }
echo '[1/4] Проверка комплекта выпуска'
download SHA256SUMS "$stage/sums"
expected=$(awk -v name="$zip" '$2 == name {print $1}' "$stage/sums")
[[ $expected =~ ^[0-9a-f]{64}$ ]] || fail 'Не удалось прочитать SHA-256 комплекта.'
verified() { [[ -f "$1" ]] && [[ $(sha256sum < "$1" | cut -d' ' -f1) = "$expected" ]]; }
if ! verified "$cache/$zip"; then
    echo 'Скачиваю ZIP с образами на эту VPS.'
    download "$zip" "$cache/$zip.part"
    verified "$cache/$zip.part" || fail 'SHA-256 архива не совпадает с контрольной суммой выпуска.'
    mv -- "$cache/$zip.part" "$cache/$zip"
fi
exec 9>&-
python3 - "$cache/$zip" "$stage" <<'PY'
from pathlib import Path
import shutil,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for name in ('source.tar.gz','images.tar','manifest.json'):
        with z.open('anet-easy/'+name) as src, (Path(sys.argv[2])/name).open('wb') as dst:
            shutil.copyfileobj(src,dst,1024*1024)
PY
tar -xzf "$stage/source.tar.gz" -C "$stage"
deploy="$stage/contrib/deploy"
echo '[2/4] Подготовка rootless Docker'
if ! python3 -c 'import tomllib' 2>/dev/null && ! python3 -c 'import tomli' 2>/dev/null; then
    apt-get update
    apt-get install -y python3-tomli
fi
sh "$deploy/scripts/rootless-docker.sh" --wizard-user
uid=$(id -u anet-vpn)
run=(runuser -u anet-vpn -- env "HOME=$(getent passwd anet-vpn | cut -d: -f6)" "XDG_RUNTIME_DIR=/run/user/$uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus")
echo '[3/4] Подключение к панели'
if [[ ${1:-} != --stdin ]]; then
    IFS= read -rs -p 'Вставьте код приглашения из панели и нажмите Enter: ' invitation </dev/tty
    printf '\n' >/dev/tty
fi
# A separate input descriptor keeps the invitation out of arguments, environment and shell history.
python3 -I - "$stage" 3<<<"$invitation" <<'PY'
import base64, binascii, json, os, re, sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID
try:
    packet=json.loads(base64.b64decode(os.fdopen(3,'rb').read(16385).strip(),validate=True))
    host,port,api,token=map(packet.__getitem__,('host','port','api','token'))
    parsed=urlsplit(api)
    assert parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
    assert parsed.path.rstrip('/').endswith('/api/v1') and not parsed.query and not parsed.fragment
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}',host)
    assert type(port) is int and 0 < port < 65536 and re.fullmatch(r'[A-Za-z0-9_-]{32,256}',token)
    node=str(UUID(packet['node_id']))
except (UnicodeError,ValueError,KeyError,TypeError,AssertionError,binascii.Error):
    sys.exit('Проверьте код приглашения. Скопируйте целиком из панели.')
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None
opener=build_opener(NoRedirect)
try:
    healthy=False
    try:
        with opener.open(api.rstrip('/')+'/node-healthz',timeout=12) as response:
            healthy=response.status==200 and response.read(64).strip()==b'ANet node API'
    except HTTPError:
        pass
    if not healthy:
        probe=Request(f"{api.rstrip('/')}/control/nodes/{node}/enroll",data=b'{}',
                      headers={'X-Enrollment-Token':'anet-connectivity-check','Content-Type':'application/json'})
        try:
            with opener.open(probe,timeout=12):
                sys.exit('Адрес API не подтвердил проверку. Проверьте HTTPS-прокси панели.')
        except HTTPError as exc:
            if exc.code != 401:
                raise
except (URLError,OSError) as exc:
    sys.exit(f'API панели недоступен с этой VPS. Проверьте DNS, сертификат и прокси /api/v1: {exc}')
root=Path(sys.argv[1])
(root/'invite').write_text(token)
(root/'join.json').write_text(json.dumps(dict(node_id=node,api=api.rstrip('/'),invitation_file=str(root/'invite'))))
(root/'node').write_text(node)
(root/'host').write_text(host)
(root/'port').write_text(str(port))
PY
unset invitation
node=$(cat "$stage/node")
host=$(cat "$stage/host")
port=$(cat "$stage/port")
chown -R anet-vpn: "$stage"
echo '[4/4] Обмен приглашения и запуск сервера'
"${run[@]}" env VPN_PORT="$port" COMPOSE_PROJECT_NAME="anet-node-$node" bash -c '
    cd "$HOME" && exec sh "$1/contrib/deploy/install.sh" --profile standalone --bundle "$1" --host "$2" \
      --state "$HOME/.local/state/anet-nodes/$3" --join-panel "$1/join.json"
' _ "$stage" "$host" "$node" ||
    fail 'Установка прервана. Проверьте вывод выше. Если приглашение истекло, выпустите новое для той же записи и повторите. Скачанный ZIP сохранён.'
echo 'Узел запущен. Откройте «Серверы» в панели и проверьте состояние узла.'
