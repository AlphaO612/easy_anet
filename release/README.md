# Установочный комплект ANet Easy

**[Скачать ANet Easy для двух VPS (ZIP)](https://github.com/AlphaO612/easy_anet/releases/download/anet-easy-2026.11.23/anet-easy-linux-amd64.zip)** · [Контрольная сумма SHA-256](https://github.com/AlphaO612/easy_anet/releases/download/anet-easy-2026.11.23/SHA256SUMS) · [Страница выпуска](https://github.com/AlphaO612/easy_anet/releases/tag/anet-easy-2026.11.23)

1. Скачайте ZIP **на компьютер** и распакуйте. Для серверов нужна Debian или Ubuntu на **amd64**.
2. Откройте терминал в распакованной папке `anet-easy`.
3. В Windows PowerShell запустите `.\send-to-vps.ps1`. В Linux, macOS или WSL запустите `bash ./send-to-vps.sh`.
4. Введите адрес, пользователя и SSH-порт первой VPS. Помощник передаст комплект и откроет мастер в этом же окне.

| Внутри ZIP | Назначение |
| --- | --- |
| `send-to-vps.ps1`, `send-to-vps.sh` | Передача с компьютера по SSH |
| `install.sh` | Запуск мастера на первой VPS |
| `images.tar`, `manifest.json` | 11 готовых образов и манифест для проверки |
| `source.tar.gz` | Исходники установщика соответствующей версии |
| `START-HERE.md` | Ответы на вопросы мастера и команды для ручной установки |

### Проверка загрузки

Скачайте `SHA256SUMS` рядом с ZIP. В Linux выполните `sha256sum -c SHA256SUMS`, в macOS — `shasum -a 256 -c SHA256SUMS`. В PowerShell выполните `Get-FileHash .\anet-easy-linux-amd64.zip -Algorithm SHA256` и сравните значение со строкой в `SHA256SUMS`.

[Пошаговая инструкция с настройкой VPN и подключением к панели](../contrib/deploy/distribution/START-HERE.md)
