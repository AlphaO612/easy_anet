#!/usr/bin/env python3
"""Publish the node API and subscription endpoint on a dedicated HTTPS hostname."""
import fcntl
import fnmatch
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.request import urlopen

NGINX = Path("/etc/nginx/conf.d")
ACME = Path("/etc/letsencrypt")


def write(path, data, mode=0o644):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w") as tmp:
        tmp.write(data)
    os.chmod(tmp.name, mode)
    os.replace(tmp.name, path)


def config(domain, port, https):
    root = "/var/www/anet-node-api/" + domain
    listen6 = Path("/proc/net/if_inet6").exists()
    def listen(port, options=""):
        return f"listen {port}{options};" + (f" listen [::]:{port}{options};" if listen6 else "")
    text = f"""# Managed by ANet Easy
server {{
    {listen(80)}
    server_name {domain};
    root {root};
    location ^~ /.well-known/acme-challenge/ {{ try_files $uri =404; }}
    location / {{ return 404; }}
}}
"""
    if https:
        text += f"""server {{
    {listen(443, " ssl")}
    server_name {domain};
    ssl_certificate {ACME}/live/anet-node-{domain}/fullchain.pem;
    ssl_certificate_key {ACME}/live/anet-node-{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /dev/null;
    location = /api/v1/node-healthz {{ default_type text/plain; return 200 "ANet node API"; }}
    location ~ ^/api/v1/(?:control/nodes/|check_access$|vless-subscriptions/[A-Za-z0-9_-]+$) {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        add_header Cache-Control no-store always;
    }}
    location / {{ return 404; }}
}}
"""
    return text


def run(*args):
    subprocess.run(args, check=True)


def reload_site():
    run("nginx", "-t")
    run("systemctl", "reload-or-restart", "nginx")


def main():
    if os.geteuid():
        sys.exit("Запустите через sudo на VPS, где установлен WebUI.")
    domain = input("Отдельное DNS-имя для узлов и подписок (A/AAAA на эту VPS): ").strip().lower()
    labels = domain.split(".")
    if len(domain) > 220 or len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels):
        raise ValueError("Введите DNS-имя, например nodes.example.org.")
    socket.getaddrinfo(domain, 443)
    port = int(input("Локальный порт панели [8088]: ").strip() or "8088")
    if not 1 <= port <= 65535:
        raise ValueError("Недопустимый порт.")
    with urlopen(f"http://127.0.0.1:{port}/", timeout=4) as response:
        if response.status != 200:
            raise ValueError("Панель недоступна на этом порте.")
    email = input("Email администратора для Let's Encrypt: ").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Проверьте email.")
    print(f"Будет создан HTTPS-сайт {domain} для узлов и подписок. Откройте TCP 80 и 443.")
    if input("Продолжить? [д/Н]: ").strip().lower() not in ("да","д","yes","y"):
        return
    packages = [name for name in ("nginx","certbot") if not shutil.which(name)]
    if packages:
        run("apt-get", "update")
        run("apt-get", "install", "-y", *packages)
    NGINX.mkdir(parents=True, exist_ok=True)
    conf = NGINX / f"anet-node-{domain}.conf"
    before = conf.read_text() if conf.exists() else None
    http, tls = config(domain, port, False), config(domain, port, True)
    if conf.is_symlink() or before not in (None, http, tls):
        raise ValueError(f"Файл {conf} содержит другие настройки.")
    dump = subprocess.check_output(["nginx", "-T"], text=True)
    sections = re.split(r"^# configuration file (.+):$", dump, flags=re.MULTILINE)
    for file, section in zip(sections[1::2], sections[2::2]):
        if Path(file) == conf:
            continue
        for names in re.findall(r"\bserver_name\s+([^;]+);", re.sub(r"#[^\n]*","",section)):
            if any(fnmatch.fnmatchcase(domain, n) or n.startswith(".") and domain.endswith(n) for n in names.split()):
                raise ValueError(f"Имя {domain} уже обслуживается в {file}. Выберите отдельное имя.")
    root = Path("/var/www/anet-node-api") / domain
    root.mkdir(parents=True, exist_ok=True)
    try:
        if before is None:
            write(conf, http)
        reload_site()
        run("certbot", "certonly", "--webroot", "-w", str(root), "-d", domain,
            "--cert-name", "anet-node-"+domain, "--email", email, "--non-interactive",
            "--agree-tos", "--keep-until-expiring")
        write(conf, tls)
        reload_site()
        with urlopen(f"https://{domain}/api/v1/node-healthz", timeout=12) as r:
            if r.read(80) != b"ANet node API":
                raise ValueError("Проверка публичного HTTPS-адреса не прошла.")
        hooks = ACME / "renewal-hooks/deploy"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / f"anet-node-{domain}"
        content = f'#!/bin/sh\n[ "$RENEWED_LINEAGE" = "{ACME}/live/anet-node-{domain}" ] || exit 0\nnginx -t && systemctl reload nginx\n'
        if hook.exists() and hook.read_text() != content:
            raise ValueError(f"Проверьте существующий hook {hook}.")
        write(hook, content, 0o755)
        run("systemctl","enable","--now","certbot.timer")
    except BaseException:
        if before is None:
            conf.unlink(missing_ok=True)
        else:
            write(conf,before)
        reload_site()
        raise
    print(f"Введите в панели публичный URL API: https://{domain}/api/v1")


if __name__ == "__main__":
    try:
        if os.geteuid() == 0:
            lock = open("/run/anet-node-api.lock", "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Настройка HTTPS прервана: {exc}")
