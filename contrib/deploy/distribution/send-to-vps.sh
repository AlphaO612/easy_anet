#!/bin/sh
set -eu
umask 077
key_only=0
case "$#:${1:-}" in 0:) ;; 1:--key-only) key_only=1;; *) echo 'Использование: send-to-vps.sh [--key-only]' >&2; exit 2;; esac
cd -- "$(dirname -- "$0")"
for command in ssh ssh-keygen base64; do
    command -v "$command" >/dev/null || { echo "Отсутствует $command. Установите клиент OpenSSH и повторите запуск." >&2; exit 1; }
done
if [ "$key_only" = 0 ]; then
    command -v scp >/dev/null || { echo 'Не найден scp.' >&2; exit 1; }
    for file in install.sh source.tar.gz manifest.json images.tar; do
        [ -s "$file" ] || { echo "В каталоге поставки отсутствует $file. Скачайте полный комплект." >&2; exit 1; }
    done
fi
printf 'Адрес первой VPS (IPv4 или DNS): '; read -r host
printf 'Пользователь SSH [root]: '; read -r user
printf 'Порт SSH [22]: '; read -r port
user=${user:-root}
port=${port:-22}
case "$host" in ''|[!A-Za-z0-9]*|*[!A-Za-z0-9.-]*) echo 'Введите IPv4 или DNS-имя VPS.' >&2; exit 2;; esac
case "$user" in ''|-*|*[!A-Za-z0-9_-]*) echo 'Проверьте имя пользователя SSH.' >&2; exit 2;; esac
case "$port" in ''|*[!0-9]*) echo 'Порт SSH должен быть числом.' >&2; exit 2;; esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || { echo 'Порт SSH должен быть от 1 до 65535.' >&2; exit 2; }
target="$user@$host"
key_dir="$HOME/.ssh/anet-easy/$(printf %s "$host" | tr '[:upper:]' '[:lower:]')/$port"
key="$key_dir/id_ed25519"
mkdir -p "$key_dir"
if [ ! -f "$key" ]; then
    echo "Создаю отдельный ключ для root@$host. Задайте фразу защиты или нажмите Enter."
    ssh-keygen -q -t ed25519 -C anet-easy -f "$key"
fi
public=$(ssh-keygen -y -f "$key")
blob=$(printf '%s\n' "$public" | awk '$1=="ssh-ed25519" && length($2)==68 && $2 ~ /^[A-Za-z0-9+\/]+$/ {print $2}')
[ -n "$blob" ] || { echo 'Не удалось прочитать публичную часть ключа Ed25519.' >&2; exit 1; }
session=$(mktemp -d /tmp/anet-ssh.XXXXXXXX)
socket=$session/control
cleanup() {
    if [ -S "$socket" ]; then ssh -S "$socket" -O exit -p "$port" "$target" >/dev/null 2>&1 || :; fi
    rmdir "$session" 2>/dev/null || :
}
trap cleanup EXIT
folder="anet-install.${session##*.}"
remote_setup=$(base64 <<'AUTHORIZED_KEY' | tr -d '\r\n'
set -eu
[ "$(id -u)" = 0 ] || exit 1
cd "$(getent passwd root | cut -d: -f6)"
umask 077
[ ! -L .ssh ] && [ ! -L .ssh/authorized_keys ] || exit 1
mkdir -p .ssh
chown root .ssh
chmod 700 .ssh
exec 9>>.ssh/authorized_keys
flock -x 9
chown root .ssh/authorized_keys
chmod 600 .ssh/authorized_keys
if ! awk -v key="$1" '{ for(i=2;i<=NF;i++) if ($(i-1)=="ssh-ed25519" && $i==key) found=1 } END{exit !found}' .ssh/authorized_keys; then
    printf '\nssh-ed25519 %s anet-easy\n' "$1" >&9
fi
AUTHORIZED_KEY
)
shell=/bin/sh
[ "$user" = root ] || shell='sudo -H /bin/sh'
remote="printf %s '$remote_setup' | base64 -d | $shell -s -- '$blob'"
if [ "$key_only" = 0 ]; then remote="umask 077; mkdir '$folder' || exit 1; $remote"; fi
echo "Подключение к $target, порт $port. Сверьте отпечаток SSH с консолью провайдера."
set -- -o StrictHostKeyChecking=ask -o ControlMaster=auto -o ControlPersist=60 -o "ControlPath=$socket"
if [ "$user" = root ]; then set -- "$@" -i "$key"; fi
# sudo can prompt on its controlling terminal while sh reads the script from the pipe.
ssh "$@" -tt -p "$port" "$target" "$remote" || {
    echo 'Запись ключа не подтверждена. Проверьте доступ по SSH и права sudo.' >&2
    [ "$user" != root ] && [ "$key_only" = 0 ] || exit 1
}
quoted_key=$(printf %s "$key" | sed "s/'/'\\\\''/g")
if ssh -F none -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o PreferredAuthentications=publickey \
        -i "$key" -p "$port" "root@$host" 'test "$(id -u)" = 0'; then
    printf "Проверен вход по отдельному ключу:\nssh -i '%s' -p %s root@%s\n" "$quoted_key" "$port" "$host"
else
    echo "Вход root по ключу не подтверждён. Проверьте разрешение входа root; действующий логин: $target." >&2
    [ "$key_only" = 0 ] || exit 1
fi
[ "$key_only" = 0 ] || exit 0
echo "Передача четырёх файлов в $target:~/$folder"
scp "$@" -P "$port" ./install.sh ./source.tar.gz ./manifest.json ./images.tar "$target:$folder/" ||
    { echo 'Не удалось передать файлы. Проверьте связь, доступ и место на VPS.' >&2; exit 1; }
echo "Чтобы вернуться в мастер с компьютера:"
if [ "$user" = root ]; then
    printf "ssh -tt -i '%s' -p %s %s 'cd %s && bash ./install.sh'\n" "$quoted_key" "$port" "$target" "$folder"
else
    echo "ssh -tt -p $port $target 'cd $folder && bash ./install.sh'"
fi
ssh "$@" -tt -p "$port" "$target" "cd $folder && bash ./install.sh"
