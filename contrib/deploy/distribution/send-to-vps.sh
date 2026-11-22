#!/bin/sh
set -eu

cd -- "$(dirname -- "$0")"
for command in ssh scp; do
    command -v "$command" >/dev/null || { echo "Отсутствует $command. Установите клиент OpenSSH и повторите запуск." >&2; exit 1; }
done
for file in install.sh source.tar.gz manifest.json images.tar; do
    [ -s "$file" ] || { echo "В каталоге поставки отсутствует $file. Скачайте полный комплект." >&2; exit 1; }
done
printf 'Адрес первой VPS (IPv4 или DNS): '; read -r host
printf 'Пользователь SSH [root]: '; read -r user
printf 'Порт SSH [22]: '; read -r port
user=${user:-root}
port=${port:-22}
case "$host" in ''|-*|*[!A-Za-z0-9.-]*) echo 'Введите IPv4 или DNS-имя VPS.' >&2; exit 2;; esac
case "$user" in ''|-*|*[!A-Za-z0-9_-]*) echo 'Проверьте имя пользователя SSH.' >&2; exit 2;; esac
case "$port" in ''|*[!0-9]*) echo 'Порт SSH должен быть числом.' >&2; exit 2;; esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || { echo 'Порт SSH должен быть от 1 до 65535.' >&2; exit 2; }
target="$user@$host"
# A fresh directory keeps an interrupted transfer away from an existing installer.
echo "[1/3] Подключение к $target, порт $port. Сверьте отпечаток SSH с консолью провайдера."
folder=$(ssh -p "$port" "$target" 'umask 077; mktemp -d anet-install.XXXXXXXX') ||
    { echo 'Не удалось подключиться. Проверьте адрес, порт, логин и способ входа.' >&2; exit 1; }
case "$folder" in anet-install.????????) ;; *) echo 'Сервер вернул неожиданный каталог.' >&2; exit 1;; esac
case "$folder" in *[!A-Za-z0-9.-]*) exit 1;; esac
echo "[2/3] Передача четырёх файлов в $target:~/$folder"
scp -P "$port" ./install.sh ./source.tar.gz ./manifest.json ./images.tar "$target:$folder/" ||
    { echo 'Передача прервана. Проверьте связь и свободное место на VPS, затем запустите этот помощник снова.' >&2; exit 1; }
echo "[3/3] Открываю мастер на первой VPS. Чтобы вернуться в него с компьютера:"
echo "ssh -tt -p $port $target 'cd $folder && bash ./install.sh'"
exec ssh -tt -p "$port" "$target" "cd $folder && bash ./install.sh"
