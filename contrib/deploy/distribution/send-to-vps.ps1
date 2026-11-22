$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($command in @('ssh', 'scp')) {
    if (!(Get-Command $command -ErrorAction SilentlyContinue)) { throw "Отсутствует $command. Установите компонент Windows «Клиент OpenSSH» и повторите запуск." }
}
$files = @('install.sh', 'source.tar.gz', 'manifest.json', 'images.tar')
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "В каталоге поставки отсутствует $file. Скачайте полный комплект." }
}
$server = Read-Host 'Адрес первой VPS (IPv4 или DNS)'
$userName = Read-Host 'Пользователь SSH [root]'
$sshPort = Read-Host 'Порт SSH [22]'
if (!$userName) { $userName = 'root' }
if (!$sshPort) { $sshPort = '22' }
if ($server -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$') { throw 'Введите IPv4 или DNS-имя VPS.' }
if ($userName -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_-]*$') { throw 'Проверьте имя пользователя SSH.' }
if ($sshPort -notmatch '^\d{1,5}$' -or [int]$sshPort -lt 1 -or [int]$sshPort -gt 65535) { throw 'Порт SSH должен быть от 1 до 65535.' }
$target = "$userName@$server"
Write-Host "[1/3] Подключение к $target, порт $sshPort. Сверьте отпечаток SSH с консолью провайдера."
$folder = ssh -p $sshPort $target 'umask 077; mktemp -d anet-install.XXXXXXXX'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подключиться. Проверьте адрес, порт, логин и способ входа.' }
if (@($folder).Count -ne 1 -or $folder -cnotmatch '^anet-install\.[a-zA-Z0-9]{8}$') { throw 'Сервер вернул неожиданный каталог.' }
Write-Host "[2/3] Передача четырёх файлов в ${target}:~/$folder"
scp -P $sshPort @files "${target}:$folder/"
if ($LASTEXITCODE -ne 0) { throw 'Передача прервана. Проверьте связь и свободное место на VPS, затем запустите этот помощник снова.' }
Write-Host '[3/3] Открываю мастер на первой VPS. Чтобы вернуться в него с компьютера:'
Write-Host "ssh -tt -p $sshPort $target 'cd $folder && bash ./install.sh'"
ssh -tt -p $sshPort $target "cd $folder && bash ./install.sh"
exit $LASTEXITCODE
