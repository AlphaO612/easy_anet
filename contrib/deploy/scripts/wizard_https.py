"""Provision a dedicated nginx site and renew its VLESS certificates on the entry VPS."""
import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time

STATE = Path("/etc/anet-wizard/https")
WWW = Path("/var/www/anet-wizard")
NGINX = Path("/etc/nginx/conf.d")
ACME = Path("/etc/letsencrypt")
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def run(*args, capture=False, **kwargs):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else sys.stderr, **kwargs)


def atomic(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        name = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fchmod(temp.fileno(), mode)
            name.replace(path)
        finally:
            name.unlink(missing_ok=True)


def trusted_directory(path, mode=0o755):
    # Each privileged parent must prevent replacement of its children by other users.
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=mode if current == path else 0o755)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Для системного каталога {current} нужен владелец root и запись только для владельца.")


def validate(domain, installation, owner, entries):
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("Нужно полное доменное имя в ASCII или Punycode.")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("Для сертификата введите доменное имя.")
    if not re.fullmatch(r"[a-f0-9]{10}", installation) or not entries or any(
            not re.fullmatch(r"entry-[1-9][0-9]*", name) for name in entries):
        raise ValueError("Неверный идентификатор установки или входа.")
    account = pwd.getpwnam(owner)
    if account.pw_uid == 0:
        raise ValueError("Укажите пользователя rootless Docker.")
    return account


def slot(spec):
    fingerprint = hashlib.sha256(spec["domain"].encode()).hexdigest()[:12]
    return "anet-" + spec["installation"] + "-" + fingerprint


def addresses(domain):
    resolved = {item[4][0] for item in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    interfaces = json.loads(run("ip", "-j", "address", "show", capture=True).stdout)
    local = {a["local"] for device in interfaces for a in device["addr_info"]}
    for value in resolved:
        ip = ipaddress.ip_address(value)
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local or (not ip.is_global and value not in local):
            raise ValueError(f"DNS {domain} указывает на неподходящий адрес {value}. Проверьте A и AAAA.")
    if not resolved:
        raise ValueError(f"Для {domain} не найдены записи A или AAAA.")
    return sorted(resolved)


def port_preflight():
    listeners = run("ss", "-H", "-ltnp", "sport = :80 or sport = :443", capture=True).stdout
    for line in listeners.splitlines():
        procs = re.findall(r'\("([^"]+)"', line)
        if not procs or set(procs) != {"nginx"}:
            raise ValueError("TCP 80 или 443 занят другой службой. Выберите отдельный вход или настройте сайт вручную.")


def check_names(dump, domain, config):
    sections = re.split(r"^# configuration file (.+):$", dump, flags=re.MULTILINE)
    for filename, text in zip(sections[1::2], sections[2::2]):
        if Path(filename) == config:
            continue
        text = re.sub(r"#[^\n]*", "", text)
        for names in re.findall(r"\bserver_name\s+([^;]+);", text):
            for name in names.lower().split():
                name = name.strip("\"'")
                if name.startswith("~"):
                    try:
                        matches = re.search(name[1:], domain) is not None
                    except re.error:
                        matches = True
                elif name.startswith("."):
                    matches = domain.endswith(name) or domain == name[1:]
                else:
                    matches = fnmatch.fnmatchcase(domain, name)
                if matches:
                    raise ValueError(f"Имя {domain} уже обслуживает другой сайт nginx. Его конфигурация сохранена.")


def nginx_site(spec, tls):
    name, domain = slot(spec), spec["domain"]
    docroot = WWW / name
    ipv6 = Path("/proc/net/if_inet6").exists()
    def listener(port, options=""):
        v4 = f"listen {port}{options};"
        return v4 + (f" listen [::]:{port}{options};" if ipv6 else "")
    http = f"""# ANet site {name}
server {{
    {listener(80)}
    server_name {domain};
    root {docroot};
    location ^~ /.well-known/acme-challenge/ {{ try_files $uri =404; }}
    location / {{ return 301 https://{domain}$request_uri; }}
}}
"""
    if tls:
        lineage = ACME / "live" / name
        subscription = ""
        if port := spec.get("subscription_port"):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Некорректный порт панели.")
            subscription = f"""    location ~ "^/api/v1/vless-subscriptions/[A-Za-z0-9_-]{{43}}$" {{
        access_log off;
        error_log /dev/null;
        limit_except GET {{ deny all; }}
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Authorization "";
        proxy_set_header Cookie "";
    }}
"""
        http += f"""server {{
    {listener(443, " ssl")}
    server_name {domain};
    ssl_certificate {lineage}/fullchain.pem;
    ssl_certificate_key {lineage}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root {docroot};
    index index.html;
{subscription}    location / {{ try_files $uri $uri/ =404; }}
}}
"""
    return http.encode()


def checked_reload():
    run("nginx", "-t")
    run("systemctl", "reload-or-restart", "nginx.service")


def request(address, domain, path, tls, certificate=None):
    with socket.create_connection((address, 443 if tls else 80), timeout=7) as raw:
        with contextlib.ExitStack() as stack:
            sock = raw
            if tls:
                sock = stack.enter_context(ssl.create_default_context().wrap_socket(raw, server_hostname=domain))
                if sock.getpeercert(binary_form=True) != certificate:
                    raise ValueError(f"На {address}:443 другой сертификат. Проверьте DNS и настройки nginx.")
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode())
            with http.client.HTTPResponse(sock) as response:
                response.begin()
                return response.status, response.read(2 * 1024 * 1024)


def verify_site(ips, domain, path, expected, certificate=None):
    tls = certificate is not None
    for address in ips:
        for attempt in range(3):
            try:
                status, body = request(address, domain, path, tls, certificate)
                if status != 200 or body != expected:
                    raise ValueError(f"ожидался файл этого сайта, HTTP {status}")
                break
            except (OSError, ValueError, http.client.HTTPException) as exc:
                if attempt == 2:
                    raise ValueError(f"Проверка {domain} через {address}:{443 if tls else 80} не прошла ({exc}). "
                                     "Проверьте A/AAAA, firewall и перенаправление портов на вход.") from exc
                time.sleep(1)


def write_owner_copies(state, spec, cert, key):
    dest = state / "tls" / spec["domain"]
    dest.mkdir(mode=0o700, exist_ok=True, parents=True)
    # Provisioning and renewal must agree on the pair a new ingress reads.
    with (state / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        atomic(dest / "fullchain.pem", cert)
        atomic(dest / "privkey.pem", key)
        for entry in spec["entries"]:
            adapter = state / entry / "adapter/server"
            if not (adapter / "server.json").is_file():
                continue
            atomic(adapter / "tls.pem", cert)
            atomic(adapter / "tls.key", key)
            project = f"anet-{spec['installation']}-{entry}"
            containers = run("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project,
                             "--filter", "label=com.docker.compose.service=ingress", capture=True).stdout.split()
            for container in containers:
                run("docker", "restart", container)


def sync_copies(spec, cert, key):
    # All paths under HOME and the rootless Docker socket belong to an unprivileged user.
    account = validate(spec["domain"], spec["installation"], spec["owner"], spec["entries"])
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            os.chdir(account.pw_dir)
            os.umask(0o077)
            runtime = f"/run/user/{account.pw_uid}"
            os.environ.clear()
            os.environ.update(HOME=account.pw_dir, PATH=PATH, XDG_RUNTIME_DIR=runtime,
                              DOCKER_HOST="unix://" + runtime + "/docker.sock")
            state = Path(account.pw_dir) / ".local/state/anet-wizard" / spec["installation"]
            write_owner_copies(state, spec, cert, key)
        except Exception as exc:
            print(f"Не удалось обновить сертификат входа: {exc}", file=sys.stderr, flush=True)
            os._exit(1)
        os._exit(0)
    if os.waitpid(pid, 0)[1] != 0:
        raise ValueError("Проверьте права пользователя rootless Docker и его daemon. Повторите установку для синхронизации сертификата.")


def apply_cert(spec):
    lineage = ACME / "live" / slot(spec)
    cert = (lineage / "fullchain.pem").read_bytes()
    key = (lineage / "privkey.pem").read_bytes()
    sync_copies(spec, cert, key)


def install(spec, email, template, github):
    from wizard_site import render_site
    html = render_site(template, github).encode()
    domain, name = spec["domain"], slot(spec)
    os.umask(0o022)
    trusted_directory(STATE, 0o700)
    with (STATE / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not shutil.which("ip") or not shutil.which("ss"):
            run("apt-get", "update")
            run("apt-get", "install", "-y", "iproute2")
        ips = addresses(domain)
        port_preflight()
        packages = [tool for tool in ("certbot", "nginx") if shutil.which(tool) is None]
        if packages:
            run("apt-get", "update")
            run("apt-get", "install", "-y", *packages, env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
        trusted_directory(NGINX)
        trusted_directory(WWW)
        conf, docroot, state = NGINX / (name + ".conf"), WWW / name, STATE / name
        lineage = ACME / "live" / name
        metadata = state / "site.json"
        hook = ACME / "renewal-hooks/deploy" / name
        if not metadata.exists():
            existing = (conf, docroot, hook, lineage, ACME / "renewal" / (name + ".conf"))
            if any(p.exists() or p.is_symlink() for p in existing):
                raise ValueError("Найдены файлы другого сайта с тем же именем. Выберите другое имя домена.")
        else:
            trusted_directory(state, 0o700)
            old = json.loads(metadata.read_text())
            if any(old[k] != spec[k] for k in ("owner", "domain", "installation")):
                raise ValueError("У сайта другой владелец или идентификатор установки.")
        old_conf = conf.read_bytes() if conf.exists() else None
        if conf.is_symlink() or old_conf not in (None, nginx_site(spec, False), nginx_site(spec, True),
                nginx_site(old, True) if metadata.exists() else None):
            raise ValueError(f"Конфигурация {conf} изменена вручную. Проверьте её перед повторным запуском.")
        check_names(run("nginx", "-T", capture=True, stderr=subprocess.PIPE).stdout, domain, conf)
        trusted_directory(state, 0o700)
        if not metadata.exists():
            atomic(metadata, json.dumps(spec).encode())
        trusted_directory(docroot)
        challenge = docroot / ".well-known/acme-challenge"
        trusted_directory(challenge)
        token = challenge / secrets.token_hex(16)
        content = secrets.token_hex(32).encode()
        old_html = (docroot / "index.html").read_bytes() if (docroot / "index.html").exists() else None
        try:
            if old_conf is None:
                atomic(conf, nginx_site(spec, False), 0o644)
            checked_reload()
            atomic(token, content, 0o644)
            verify_site(ips, domain, "/.well-known/acme-challenge/" + token.name, content)
            print("HTTP-01 доступен. Запрашиваю сертификат.", file=sys.stderr)
            with tempfile.NamedTemporaryFile(dir=state, mode="w") as cfg:
                defaults = ACME / "cli.ini"
                if defaults.is_file():
                    for line in defaults.read_text().splitlines():
                        if not re.match(r"^\s*email\s*[:=]", line, flags=re.I):
                            cfg.write(line + "\n")
                cfg.write(f"email = {email}\n")
                cfg.flush()
                run("certbot", "certonly", "--config", cfg.name, "--webroot", "-w", str(docroot), "--non-interactive",
                    "--agree-tos", "--keep-until-expiring", "--no-directory-hooks", "--cert-name", name, "-d", domain)
            atomic(docroot / "index.html", html, 0o644)
            atomic(conf, nginx_site(spec, True), 0o644)
            checked_reload()
            der = ssl.PEM_cert_to_DER_cert((lineage / "cert.pem").read_text())
            verify_site(ips, domain, "/", html, der)
            atomic(state / "renew.py", Path(__file__).read_bytes())
            trusted_directory(hook.parent)
            # The timer executes only the root-owned copy, in isolated Python mode.
            atomic(hook, (f'#!/bin/sh\n[ "$RENEWED_LINEAGE" = "{lineage}" ] || exit 0\n'
                          f'exec /usr/bin/python3 -I "{state}/renew.py" --renew\n').encode(), 0o755)
            run("systemctl", "enable", "--now", "certbot.timer")
            run("systemctl", "enable", "nginx.service")
            apply_cert(spec)
            atomic(metadata, json.dumps(spec).encode())
        except BaseException:
            if old_conf is None:
                conf.unlink(missing_ok=True)
            else:
                atomic(conf, old_conf, 0o644)
            if old_html is None:
                (docroot / "index.html").unlink(missing_ok=True)
            else:
                atomic(docroot / "index.html", old_html, 0o644)
            try:
                checked_reload()
            except subprocess.SubprocessError:
                print("Проверьте восстановленную конфигурацию командой nginx -t.", file=sys.stderr)
            raise
        finally:
            token.unlink(missing_ok=True)


def main():
    if os.geteuid() != 0:
        sys.exit("Для настройки nginx и Certbot запустите помощник через sudo.")
    os.environ["PATH"] = PATH
    if sys.argv[1:] == ["--renew"]:
        spec = json.loads(Path(__file__).with_name("site.json").read_text())
        validate(spec["domain"], spec["installation"], spec["owner"], spec["entries"])
        if os.environ.get("RENEWED_LINEAGE") != str(ACME / "live" / slot(spec)):
            return
        with (STATE / "install.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            checked_reload()
            apply_cert(spec)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("domain", "owner", "installation"):
        parser.add_argument("--" + name, required=True)
    email = parser.add_mutually_exclusive_group(required=True)
    email.add_argument("--email")
    email.add_argument("--email-file", type=Path)
    parser.add_argument("--template", choices=["studio", "journal", "github", "default"], required=True)
    parser.add_argument("--github", "--github-user", default="")
    parser.add_argument("--subscription-port", type=int)
    parser.add_argument("--entry", action="append", required=True)
    args = parser.parse_args()
    domain = args.domain.lower()
    validate(domain, args.installation, args.owner, args.entry)
    address = args.email if args.email is not None else args.email_file.read_text().strip()
    if len(address) > 254 or not re.fullmatch(r"[a-zA-Z0-9._+%-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,63}", address):
        raise ValueError("Введите контактный email для Let's Encrypt.")
    install(dict(domain=domain, owner=args.owner, installation=args.installation, entries=sorted(set(args.entry)),
                 subscription_port=args.subscription_port),
            address, args.template, args.github)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
