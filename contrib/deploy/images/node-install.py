"""Run the fixed release installer on an administrator-selected, pinned SSH server."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread, Timer
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

AUTH_SERVERS = "http://auth:3000/api/v1/servers"
INSTALLER = Path(__file__).with_name("join-node.sh")
JOBS = {}
LOCK = Lock()
PATH = re.compile(r"/api/v1/node-install/([0-9a-f-]{36})/(fingerprint|start|status)")

class Failure(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def find_server(admin, node_id):
    if not admin or not admin.startswith("Bearer "):
        raise Failure("Войдите в панель.", 401)
    request = Request(AUTH_SERVERS, headers={"Authorization": admin})
    try:
        with urlopen(request, timeout=8) as response:
            servers = json.load(response)
    except HTTPError as err:
        raise Failure("Панель отклонила сессию администратора.", 403 if err.code in (401, 403) else 502) from None
    except (URLError, ValueError, OSError):
        raise Failure("Не удалось проверить сессию в API панели.", 502) from None
    for server in servers:
        if server["id"] == node_id:
            return server
    raise Failure("Запись VPS отсутствует. Обновите список серверов.", 404)


def ssh_target(server):
    host = server["address"]
    user = server.get("ssh_user") or "root"
    port = server.get("ssh_port") or 22
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}", host) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}", user) or not 1 <= port <= 65535:
        raise Failure("Исправьте адрес, логин и порт SSH в карточке сервера.")
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(item[4][0]) for item in answers]
    except (OSError, ValueError):
        raise Failure("Не удалось определить IP-адрес VPS.") from None
    if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
        raise Failure("Для установки из панели укажите публичный адрес VPS. Для частной сети используйте ручной режим.")
    return str(addresses[0]), port, user


def scan(ip, port):
    try:
        result = subprocess.run(["ssh-keyscan", "-T", "6", "-t", "ed25519", "-p", str(port), ip],
                                capture_output=True, timeout=9)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == b"ssh-ed25519":
                key = base64.b64decode(parts[2], validate=True)
                if len(key) == 51 and key[:19] == b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
                    fingerprint = base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
                    return "SHA256:" + fingerprint, parts[2].decode()
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    raise Failure("VPS не вернула ключ ED25519. Проверьте SSH-порт, firewall и sshd.", 502)


def update(node, state, detail):
    with LOCK:
        JOBS[node] = (monotonic(), {"state": state, "detail": detail})


# Read the two secrets from the encrypted channel. The password is passed to sudo
# only through a pipe. The release helper and invitation live in a private directory.
REMOTE = """set -eu
umask 077
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
IFS= read -r pw
IFS= read -r invite
printf '%s\\n' "$invite" > "$stage/invite"
unset invite
cat > "$stage/join"
if [ "$(id -u)" = 0 ]; then
    unset pw; bash "$stage/join" --stdin < "$stage/invite"
elif sudo -n true 2>/dev/null; then
    unset pw; sudo -n bash "$stage/join" --stdin < "$stage/invite"
else
    printf '%s\\n' "$pw" | sudo -S -p '' -- sh -c 'exec bash "$1/join" --stdin < "$1/invite"' sh "$stage"
fi
"""


def run_job(node, target, hostkey, password, packet):
    ip, port, user = target
    try:
        with tempfile.TemporaryDirectory(prefix="anet-ssh-") as tmp:
            known = Path(tmp) / "known_hosts"
            known.write_text(f"anet-target ssh-ed25519 {hostkey}\n")
            with tempfile.TemporaryFile(dir=tmp) as secret, tempfile.TemporaryFile(dir=tmp) as input_file:
                secret.write(password.encode() + b"\n")
                secret.seek(0)
                sudo_password = b"" if user == "root" else password.encode()
                input_file.write(sudo_password + b"\n" + packet + b"\n" + INSTALLER.read_bytes())
                input_file.seek(0)
                command = ["sshpass", "-d", str(secret.fileno()), "ssh", "-F", "/dev/null", "-T",
                           "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                           "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
                           "-o", "HostKeyAlias=anet-target", "-o", f"UserKnownHostsFile={known}",
                           "-o", "GlobalKnownHostsFile=/dev/null", "-o", "PreferredAuthentications=password,keyboard-interactive",
                           "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1", "-p", str(port),
                           "-l", user, ip, REMOTE]
                with subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      pass_fds=(secret.fileno(),), start_new_session=True) as proc:
                    def terminate():
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    timer = Timer(1800, terminate)
                    timer.start()
                    try:
                        stages = {b"[1/4]": "1/4 · Проверка и загрузка релиза на VPS.",
                                  b"[2/4]": "2/4 · Подготовка rootless Docker.",
                                  b"[3/4]": "3/4 · Подключение к панели.",
                                  b"[4/4]": "4/4 · Регистрация и запуск узла."}
                        for line in iter(lambda: proc.stdout.readline(4096), b""):
                            if line[:5] in stages:
                                update(node, "running", stages[line[:5]])
                        result = proc.wait()
                    finally:
                        timer.cancel()
        if result == 0:
            update(node, "done", "Установщик завершён. Проверьте состояние узла в списке серверов.")
        elif result == 5:
            update(node, "failed", "SSH отклонил пароль. Проверьте логин и пароль VPS.")
        else:
            update(node, "failed", "Установка прервана. Проверьте SSH, права sudo, доступ к GitHub и HTTPS API. При истечении приглашения выпустите новое.")
    except (OSError, ValueError, subprocess.SubprocessError):
        update(node, "failed", "Не удалось запустить SSH-установщик. Проверьте сервис node-install.")


def start(server, data, target, hostkey):
    if (server.get("runtime") or {}).get("status") == "online":
        raise Failure("Эта нода уже подключена.", 409)
    password = data.get("password")
    token = data.get("token")
    api = data.get("api")
    if not isinstance(password, str) or not 1 <= len(password) <= 1024 or any(c in password for c in "\0\n\r"):
        raise Failure("Введите SSH-пароль одной строкой.")
    if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,256}", token):
        raise Failure("Выпустите новое приглашение для этой VPS.")
    if not isinstance(api, str):
        raise Failure("Введите доступный с новой VPS HTTPS URL API панели.")
    site = urlsplit(api)
    if site.scheme != "https" or not site.hostname or site.username or site.password or site.query or site.fragment or site.path.rstrip("/") != "/api/v1":
        raise Failure("Введите HTTPS URL вида https://panel.example.org/api/v1.")
    node = server["id"]
    packet = base64.b64encode(json.dumps(dict(node_id=node, token=token, api=api, host=server["address"],
                                              port=server.get("quic_port") or 4519)).encode())
    with LOCK:
        for expired in [k for k, (t, job) in JOBS.items() if job["state"] != "running" and t < monotonic()-3600]:
            del JOBS[expired]
        if JOBS.get(node, (0, {}))[1].get("state") == "running":
            raise Failure("Для этой VPS установка уже выполняется.", 409)
        if sum(job["state"] == "running" for _, job in JOBS.values()) >= 4:
            raise Failure("Четыре установки уже выполняются. Дождитесь завершения одной из них.", 429)
        job = {"state": "running", "detail": "Подключение по SSH и проверка доступа."}
        JOBS[node] = (monotonic(), job)
    Thread(target=run_job, args=(node, target, hostkey, password, packet), daemon=True).start()
    return job


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def serve(self):
        status = 200
        try:
            if self.path == "/health" and self.command == "GET":
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            match = PATH.fullmatch(self.path)
            if not match:
                raise Failure("Страница отсутствует.", 404)
            node, action = match.groups()
            if self.command != ("GET" if action == "status" else "POST"):
                raise Failure("Метод запрещён.", 405)
            server = find_server(self.headers.get("Authorization"), node)
            if action == "status":
                with LOCK:
                    result = JOBS.get(node, (0, {"state": "idle", "detail": ""}))[1]
            else:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 8192:
                    raise Failure("Слишком большой запрос.", 413)
                if self.headers.get_content_type() != "application/json":
                    raise Failure("Требуется JSON.", 415)
                data = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(data, dict):
                    raise Failure("Проверьте значения полей.")
                target = ssh_target(server)
                fingerprint, key = scan(*target[:2])
                if action == "fingerprint":
                    result = {"fingerprint": fingerprint, "address": target[0]}
                else:
                    if data.get("fingerprint") != fingerprint:
                        raise Failure("Отпечаток изменился. Снова сверьте его с консолью провайдера.", 409)
                    result = start(server, data, target, key)
                    status = 202
        except Failure as err:
            status, result = err.code, {"error": str(err)}
        except (ValueError, KeyError, TypeError, OSError):
            status, result = 400, {"error": "Проверьте формат запроса и параметры подключения."}
        encoded = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_POST = do_GET = serve


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8092), Handler).serve_forever()
