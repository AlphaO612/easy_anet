param([switch]$KeyOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$commands = @('ssh', 'ssh-keygen')
if (!$KeyOnly) { $commands += 'scp' }
foreach ($command in $commands) {
    if (!(Get-Command $command -ErrorAction SilentlyContinue)) { throw "Отсутствует $command. Установите компонент Windows «Клиент OpenSSH»." }
}
$files = @('install.sh', 'source.tar.gz', 'manifest.json', 'images.tar')
if (!$KeyOnly) {
    foreach ($file in $files) {
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "В каталоге поставки отсутствует $file. Скачайте полный комплект." }
    }
}
$server = Read-Host 'Адрес первой VPS (IPv4 или DNS)'
$userName = Read-Host 'Пользователь SSH [root]'
$sshPort = Read-Host 'Порт SSH [22]'
if (!$userName) { $userName = 'root' }
if (!$sshPort) { $sshPort = '22' }
if ($server -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$') { throw 'Введите IPv4 или DNS-имя VPS.' }
if ($userName -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_-]*$') { throw 'Проверьте имя пользователя SSH.' }
if ($sshPort -notmatch '^[0-9]{1,5}$' -or [int]$sshPort -lt 1 -or [int]$sshPort -gt 65535) { throw 'Порт SSH должен быть от 1 до 65535.' }
$target = "$userName@$server"
$keyDir = Join-Path $HOME ('.ssh\anet-easy\' + $server.ToLowerInvariant() + '\' + $sshPort)
$key = Join-Path $keyDir 'id_ed25519'
[IO.Directory]::CreateDirectory($keyDir) | Out-Null
if (!(Test-Path -LiteralPath $key -PathType Leaf)) {
    Write-Host "Создаю отдельный ключ для root@$server. Задайте фразу защиты или нажмите Enter."
    ssh-keygen -q -t ed25519 -C anet-easy -f $key
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать локальный ключ.' }
}
$public = ssh-keygen -y -f $key
if ($LASTEXITCODE -ne 0 -or $public -cnotmatch '^ssh-ed25519 ([A-Za-z0-9+/]{68})(?:\s|$)') { throw 'Не удалось прочитать публичную часть ключа Ed25519.' }
$blob = $Matches[1]
$authorize = @'
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
'@
$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($authorize -replace "`r", '') + "`n"))
$remoteShell = '/bin/sh'
if ($userName -cne 'root') { $remoteShell = 'sudo -H /bin/sh' }
$remote = "printf %s '$payload' | base64 -d | $remoteShell -s -- '$blob'"
$folder = 'anet-install.' + [guid]::NewGuid().ToString('N')
if (!$KeyOnly) { $remote = "umask 077; mkdir '$folder' || exit 1; $remote" }
Write-Host "Подключение к $target, порт $sshPort. Сверьте отпечаток SSH с консолью провайдера."
$loginOptions = @('-o','StrictHostKeyChecking=ask')
if ($userName -ceq 'root') { $loginOptions += @('-i',$key) }
ssh @loginOptions -tt -p $sshPort $target $remote
if ($LASTEXITCODE -ne 0) {
    if ($userName -ceq 'root' -or $KeyOnly) { throw 'Не удалось записать ключ. Проверьте доступ по SSH и права sudo.' }
    Write-Warning 'Запись ключа не подтверждена. Проверьте доступ по SSH и права sudo.'
}
# A fresh connection with only this identity verifies root independently of the bootstrap login.
$uid = ssh -F none -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o PreferredAuthentications=publickey -i $key -p $sshPort "root@$server" 'id -u'
$quotedKey = "'" + $key.Replace("'", "''") + "'"
if ($LASTEXITCODE -eq 0 -and $uid -ceq '0') {
    Write-Host "Проверен вход по отдельному ключу:`nssh -i $quotedKey -p $sshPort root@$server"
} else {
    $message = "Вход root по ключу не подтверждён. Проверьте разрешение входа root; действующий логин: $target."
    if ($KeyOnly) { throw $message }
    Write-Warning $message
}
if ($KeyOnly) { exit 0 }
Write-Host "Передача четырёх файлов в ${target}:~/$folder"
scp @loginOptions -P $sshPort @files "${target}:$folder/"
if ($LASTEXITCODE -ne 0) { throw 'Не удалось передать файлы. Проверьте связь, доступ и место на VPS.' }
Write-Host 'Чтобы вернуться в мастер с компьютера:'
$identityOption = ''
if ($userName -ceq 'root') { $identityOption = "-i $quotedKey " }
Write-Host "ssh -tt ${identityOption}-p $sshPort $target 'cd $folder && bash ./install.sh'"
ssh @loginOptions -tt -p $sshPort $target "cd $folder && bash ./install.sh"
exit $LASTEXITCODE
