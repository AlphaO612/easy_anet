"""Interactive multi-VPS installation from a verified, locally delivered Linux bundle."""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import pwd
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import tempfile

from topology import atomic_write
from wizard_ui import Terminal, WizardError, failure_hint
from wizard_tls import dns_name
import wizard_edit
from wizard_edit import active_profiles, hostname, normalize, validate
from wizard_recovery import connect, credential_session, prompt_for_new_node, recovery_apply, resume_commands

ui = Terminal()

HERE = Path(__file__).resolve().parent
CONTRIB = HERE.parents[1]
STATE = Path.home() / ".local/state/anet-wizard"
PROTOCOLS = ("wireguard", "vless", "anet")
LABEL = {"wireguard": "WireGuard", "vless": "VLESS", "anet": "ANet"}
EXT = {"wireguard": "conf", "vless": "uri", "anet": "toml"}


def ask(label, default="", check=bool):
    while True:
        answer = input(label + (f" [{default}]" if default != "" else "") + ": ").strip()
        value = answer or str(default)
        if check(value):
            return value
        print("Проверьте введённое значение.")


def number(label, default, low, high):
    return int(ask(label, default, lambda s: s.isascii() and s.isdigit() and low <= int(s) <= high))


def plan():
    expert = ui.menu("Настройки установки", [
        (1, "Рекомендуемые", "автоматический выбор портов и распределение выходов"),
        (2, "Расширенные", "свои порты, SSH-адреса и выход для каждого профиля")
    ], 1) == 2
    print("Вход принимает клиентские подключения, выход определяет адрес в интернете. Панель работает на текущем VPS.")
    exits = number("Сколько выходных VPS", 1, 1, 16)
    entries = number("Сколько входных VPS", 1, 1, 16)
    local_role = number("Роль текущего VPS: 1 — вход, 2 — выход", 1, 1, 2)
    local = 0 if local_role == 1 else entries
    servers = []
    profiles = []
    for i in range(entries + exits):
        role = f"Вход {i + 1}" if i < entries else f"Выход {i - entries + 1}"
        print("\n" + role + (" (текущий VPS)" if i == local else ""))
        host = ask("Публичный IPv4 или DNS", check=hostname)
        s = {"host": host, "local": i == local}
        s["address"] = ask("Адрес SSH", host, hostname) if expert else host
        s["port"] = number("Порт SSH", 22, 1, 65535)
        s["user"] = pwd.getpwuid(os.getuid()).pw_name if s["local"] else ask("Пользователь SSH", "root", lambda v: re.fullmatch(r"[a-z_][a-z0-9_-]*", v))
        if not s["local"]:
            mode = number("Авторизация SSH: 1 — ключ/ssh-agent, 2 — пароль", 1, 1, 2)
            s["password"] = mode == 2
            if mode == 1:
                s["key"] = ask("Путь к ключу (Enter — стандартный ключ или агент)", check=lambda v: not v or Path(v).expanduser().is_file())
            prompt_for_new_node(s)
        servers.append(s)
        if i < entries:
            before = len(profiles)
            while len(profiles) == before:
                for proto in PROTOCOLS:
                    count = number(f"Сколько профилей {LABEL[proto]} на этом входе (0 — пропустить)", int(proto == "wireguard"), 0, 16)
                    for _ in range(count):
                        target = len(profiles) % exits
                        if expert and exits > 1:
                            target = number("Номер выхода", target + 1, 1, exits) - 1
                        profiles.append(dict(entry=i, exit=entries+target, protocol=proto))
                if len(profiles) == before:
                    print("Выберите хотя бы один профиль.")
            if any(p["protocol"] == "vless" for p in profiles[before:]):
                s["tls_domain"] = ask("Домен VLESS (DNS должен указывать на этот вход)",
                                      host if dns_name(host) else "", dns_name).lower()
                choice = ui.menu("Сертификат для VLESS", [
                    (1, "Let's Encrypt", "выпустить и продлевать автоматически; открыть HTTPS-сайт"),
                    (2, "Своя пара PEM", "использовать имеющийся сертификат и ключ на входном VPS")
                ], 1, new_screen=False)
                s["tls_mode"] = "acme" if choice == 1 else "existing"
                if choice == 1:
                    print("Для выпуска сертификата откройте TCP 80 и 443. Проверьте записи A и AAAA домена.")
                    s["email"] = ask("Email администратора для Let's Encrypt",
                                     check=lambda v: len(v) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v))
                    template = ui.menu("Страница на HTTPS-порту 443", [
                        (1, "Студия", "обычный статический сайт"), (2, "Журнал", "статическая страница журнала"),
                        (3, "GitHub-профиль", "страница по публичным данным пользователя GitHub")
                    ], 1, new_screen=False)
                    s["template"] = ("studio", "journal", "github")[template - 1]
                    if template == 3:
                        s["github"] = ask("Имя публичного GitHub-профиля", check=lambda v: len(v) <= 39
                                          and re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", v))
                else:
                    s["cert"] = ask("Абсолютный путь к PEM-цепочке на этом VPS", check=lambda v: v.startswith("/"))
                    s["keyfile"] = ask("Абсолютный путь к её закрытому ключу", check=lambda v: v.startswith("/"))
    missing = set(range(entries, entries + exits)) - {p["exit"] for p in profiles}
    if missing:
        raise ValueError("Для каждого выхода нужен хотя бы один входной профиль. Запустите мастер ещё раз и назначьте профиль каждому выходу.")
    port = number("Первый публичный VPN-порт", 48000, 1024, 63000) if expert else 48000
    panel = number("Порт панели (loopback)", 8088, 1024, 65535) if expert else 8088
    return normalize(dict(installation=secrets.token_hex(5), servers=servers, profiles=profiles, local=local, base=port, panel=panel))


class Host:
    def __init__(self, node, socket):
        self.node = node
        self.ssh = []
        if not node["local"]:
            self.ssh = ["ssh", "-p", str(node["port"]), "-o", "ControlPath=" + str(socket),
                        "-o", "ConnectTimeout=15"]
            if node.get("key"):
                self.ssh += ["-o", "IdentitiesOnly=yes", "-i", str(Path(node["key"]).expanduser())]
            self.target = node["user"] + "@" + node["address"]

    def __enter__(self):
        if self.ssh:
            with ui.activity(f"Вход по SSH на {self.target}, порт {self.node['port']}", interactive=True):
                connect(self.node, self.ssh, self.target)
        return self

    def __exit__(self, *_):
        if self.ssh:
            subprocess.run([*self.ssh, "-O", "exit", self.target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self, script, *, deploy=False, tty=False, **kwargs):
        script = 'cd "$HOME"\n' + script
        if deploy and self.node["user"] == "root":
            script = ('u=$(id -u anet-vpn) && exec runuser -u anet-vpn -- env '
                      'XDG_RUNTIME_DIR="/run/user/$u" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$u/bus" '
                      'sh -ec ' + shlex.quote(script))
        args = ["sh", "-ec", script]
        if self.ssh:
            args = [*self.ssh, "-o", "BatchMode=yes", *(["-tt"] if tty else ["-T"]), self.target, shlex.join(args)]
        return subprocess.run(args, check=True, **kwargs)


def sources(out):
    # Only runtime code distributed by this release may cross to another VPS.
    names = ["deploy/scripts/" + x for x in (
        "wizard_node.py", "wizard_tls.py", "bundle.py", "release.py", "topology.py", "init-state.py",
        "register-local-node.py", "init-probe-signing.py")]
    names += ["deploy/compose/" + x for x in ("compose.yaml", "chain.yaml", "webui.yaml", "probe-management.yaml")]
    names += ["chain/issue-ingress.py", "chain/ingress.yaml"]
    with tarfile.open(out, "w:gz") as tar:
        for name in names:
            tar.add(CONTRIB / name, name, recursive=False)


def prepare(host, dest, bundle, source, profile, ident, wireguard):
    remote = shlex.quote(dest)
    # sudo/SSH read their own controlling terminal. No passwords enter the plan.
    with (HERE / "rootless-docker.sh").open("rb") as script:
        host.run('umask 077; mkdir -p ' + remote + '; cat > ' + remote + '/bootstrap.sh', stdin=script)
    bootstrap = 'sh ' + remote + '/bootstrap.sh --wizard-user'
    if host.node["user"] != "root":
        bootstrap = 'if ! docker info --format "{{json .SecurityOptions}}" 2>/dev/null | grep -q name=rootless; then ' + bootstrap + '; fi'
    if wireguard:
        privileged = '' if host.node["user"] == 'root' else 'sudo '
        bootstrap += (f'; if ! /usr/bin/python3 -c "import cryptography" 2>/dev/null; then '
                      f'{privileged}apt-get update && {privileged}apt-get install -y python3-cryptography; fi')
    try:
        with ui.activity("Проверка Docker и системных пакетов; возможен запрос sudo", interactive=True):
            host.run(bootstrap, tty=True)
    except subprocess.CalledProcessError:
        raise WizardError("Подготовка Docker завершилась ошибкой. Проверьте вывод установки пакетов выше, права sudo и пользовательскую сессию systemd.") from None
    with source.open("rb") as archive:
        host.run('umask 077; mkdir -p ' + remote + '/src; tar xzf - -C ' + remote + '/src', deploy=True, stdin=archive)
    if host.node["local"] and host.node["user"] != "root":
        location = str(bundle)
    else:
        location = dest + "/bundle"
        quoted = shlex.quote(location)
        # Write each archive atomically. Identical completed uploads can be reused.
        expected = json.loads((bundle / "manifest.json").read_text())["sha256"]
        if not re.fullmatch("[a-f0-9]{64}", expected):
            raise ValueError("Неверный SHA-256 манифеста")
        host.run("umask 077; mkdir -p " + quoted, deploy=True)
        for name in ("images.tar", "manifest.json"):
            target = quoted + "/" + name
            if name == "images.tar":
                with ui.activity(f"Проверка имеющегося архива на {host.node['host']}"):
                    exists = host.run('if test -f ' + target + '; then sha256sum ' + target + '; fi', deploy=True, capture_output=True).stdout
                if exists.split()[:1] == [expected.encode()]:
                    continue
            size = (bundle / name).stat().st_size
            amount = f"{size / (1024 * 1024):.0f} МиБ" if size >= 1024 * 1024 else f"{size} Б"
            with (bundle / name).open("rb") as src, ui.activity(
                    f"Передача {name} на {host.node['host']} · {amount}", progress=lambda: src.tell() / size if size else 1):
                host.run("umask 077; cat > " + target + ".part && mv " + target + ".part " + target, deploy=True, stdin=src, stderr=subprocess.PIPE)
        location = "~/" + location
    job(host, dest, ident, action="images", profile=profile, bundle=location)


def setup_vless(host, dest, ident, names, panel_port=None):
    node = host.node
    mode = node["tls_mode"]
    # Keep the privileged helper in the login user's private installation
    # directory, separate from the rootless daemon user's copy of the runtime.
    helpers = ["wizard_https.py", "wizard_site.py"] if mode == "acme" else ["wizard_tls.py"]
    remote = dest + "/https"
    checksums = []
    for filename in helpers:
        code = (HERE / filename).read_bytes()
        checksums.append(f"{hashlib.sha256(code).hexdigest()}  {filename}")
        host.run("umask 077; mkdir -p " + shlex.quote(remote) + "; cat > " +
                 shlex.quote(remote + "/" + filename), input=code)
    args = ["--owner",
            "anet-vpn" if node["user"] == "root" else node["user"], "--installation", ident,
            "--domain", node["tls_domain"]]
    if mode == "acme":
        email_file = remote + "/acme-contact"
        host.run("umask 077; cat > " + shlex.quote(email_file), input=(node["email"] + "\n").encode())
        args += ["--email-file", email_file, "--template", node["template"]]
        if panel_port is not None:
            args += ["--subscription-port", str(panel_port)]
        if node["template"] == "github":
            args += ["--github", node["github"]]
        for name in names:
            args += ["--entry", name]
    else:
        args += ["--cert", node["cert"], "--key", node["keyfile"]]
    # Pin the bytes before root imports local Python modules. The login user
    # can write the upload directory; only root may write the verified copy.
    steps = ["PATH=/usr/sbin:/usr/bin:/sbin:/bin", "umask 077",
             "stage=$(mktemp -d /run/anet-wizard-tls.XXXXXXXX)",
             """trap 'rm -rf -- "$stage"' EXIT"""]
    for filename in helpers:
        steps.append(f'install -m 0400 -- {shlex.quote(remote + "/" + filename)} "$stage/{filename}"')
    steps.append('(cd "$stage" && ' + shlex.join(["printf", "%s\\n", *checksums]) + " | sha256sum -c --status)")
    steps.append(f'/usr/bin/python3 -Es "$stage/{helpers[0]}" ' + shlex.join(args))
    cmd = ["sh", "-ec", "\n".join(steps)]
    if node["user"] != "root":
        cmd.insert(0, "sudo")
    label = "Сайт HTTPS и сертификат Let's Encrypt" if mode == "acme" else "Импорт существующего сертификата"
    try:
        with ui.activity(f"{label} · {node['tls_domain']}", interactive=True):
            host.run(shlex.join(cmd), tty=True)
    except subprocess.CalledProcessError:
        hint = ("Проверьте A/AAAA домена, входящие TCP 80/443 и вывод nginx/certbot."
                if mode == "acme" else "Проверьте пути, соответствие сертификата и ключа и доступ через sudo.")
        raise WizardError(f"{label} для {node['tls_domain']}. {hint}") from None
    finally:
        if mode == "acme":
            try:
                host.run("rm -f -- " + shlex.quote(email_file))
            except subprocess.CalledProcessError:
                print("Не удалось удалить приватный контактный файл ACME на входной VPS.", file=sys.stderr)


def job(target, dest, ident, **task):
    raw = json.dumps(dict(task, installation=ident)).encode()
    action = task["action"]
    labels = {"images": "Проверка и загрузка образов", "exit": "Запуск выхода ANet",
              "entry": "Запуск входа и проверка маршрута", "panel": "Запуск панели и выдача профилей"}
    help_text = {
        "images": "Проверьте целостность комплекта, свободное место и доступ к rootless Docker.",
        "exit": "Проверьте UDP-порт выхода и состояние сервисов Docker.",
        "entry": "Проверьте доступность UDP-порта выхода, поддержку TUN и выбранного входного протокола.",
        "panel": "Проверьте порт панели и состояние PostgreSQL и auth."
    }
    label = f"{labels[action]} · {target.node['host']}"
    with tempfile.TemporaryFile() as diagnostics, ui.activity(label):
        try:
            result = target.run("/usr/bin/python3 " + shlex.quote(dest + "/src/deploy/scripts/wizard_node.py"),
                              deploy=True, input=raw, stdout=subprocess.PIPE, stderr=diagnostics)
        except subprocess.CalledProcessError:
            diagnostics.seek(max(0, diagnostics.tell() - 8192))
            hint = failure_hint(diagnostics.read(), help_text[action])
            project = task.get("name")
            detail = f" Проект Compose: anet-{ident}-{project}." if project else ""
            raise WizardError(f"{label}. {hint}{detail}") from None
    return json.loads(result.stdout)


def apply(spec, bundle):
    normalize(spec)
    validate(spec, for_apply=True)
    profiles = active_profiles(spec)
    used = {spec["local"]} | {p[side] for p in profiles for side in ("entry", "exit")}
    ident = spec["installation"]
    directory = STATE / ident
    directory.mkdir(mode=0o700, exist_ok=True)
    dest = ".local/share/anet-wizard/" + ident
    with tempfile.TemporaryDirectory(prefix="anet-ssh-") as tmp, ExitStack() as stack:
        ui.stage(3, "Проверка доступа к VPS")
        hosts = {i: stack.enter_context(Host(spec["servers"][i], Path(tmp) / str(i))) for i in sorted(used)}
        fingerprints = set()
        pins_file = directory / "runtime-owners.json"
        pins = json.loads(pins_file.read_text()) if pins_file.exists() else {}
        observed = {}
        for index, h in hosts.items():
            try:
                fp = h.run('test "$(uname -m)" = x86_64; test -c /dev/net/tun; cat /etc/machine-id', capture_output=True).stdout.strip()
            except subprocess.CalledProcessError:
                raise WizardError(f"{h.node['host']}: требуется Linux amd64, /dev/net/tun и постоянный machine-id.") from None
            if not fp or fp in fingerprints:
                raise ValueError("Каждая роль требует отдельный VPS с уникальным machine-id.")
            fingerprints.add(fp)
            owner = "anet-vpn" if h.node["user"] == "root" else h.node["user"]
            identity = {"machine": hashlib.sha256(fp).hexdigest(), "owner": owner}
            if str(index) in pins and pins[str(index)] != identity:
                raise WizardError(f"{h.node['host']}: прежний запуск начат на другом VPS или от другой учётной записи Docker. "
                                  "Исправьте SSH-доступ к прежнему узлу в редакторе плана.")
            observed[index] = identity

        def pin(index):
            if str(index) not in pins:
                pins[str(index)] = observed[index]
                atomic_write(pins_file, json.dumps(pins))

        ui.stage(4, "Подготовка узлов и доставка образов")
        source = Path(tmp) / "runtime.tar.gz"
        sources(source)
        for i,h in hosts.items():
            print(f"Подготовка {h.node['host']}…", flush=True)
            entry = any(p["entry"] == i for p in profiles)
            profile = "anet-easy" if entry else "webui" if i == spec["local"] else "standalone"
            prepare(h, dest, bundle, source, profile, ident,
                    any(p["entry"] == i and p["protocol"] == "wireguard" for p in profiles))
            if h.node.get("tls_mode") and any(p["entry"] == i and p["protocol"] == "vless" for p in profiles):
                names = [f"entry-{row['slot']}" for row in profiles
                         if row["entry"] == i and row["protocol"] == "vless"]
                setup_vless(h, dest, ident, names, spec["panel"] if i == spec["local"] else None)
        ui.stage(5, "Запуск VPN и проверка маршрутов")
        catalog = []
        for p in profiles:
            slot = p["slot"]
            entry, exit_host = hosts[p["entry"]], hosts[p["exit"]]
            port = spec["base"] + slot - 1
            proto = p["protocol"]
            pin(p["exit"])
            uplink = job(exit_host, dest, ident, action="exit", name=f"exit-{slot}",
                         native_port=port, host=exit_host.node["host"])["config"]
            tls = {}
            endpoint = entry.node.get("tls_domain", entry.node["host"]) if proto == "vless" else entry.node["host"]
            if proto == "vless" and entry.node.get("tls_mode"):
                tls["tls_mode"] = entry.node["tls_mode"]
            elif proto == "vless":
                # Resume plans recorded before local certificate staging.
                for key, src in (("cert", "cert"), ("key", "keyfile")):
                    tls[key] = entry.run("cat -- " + shlex.quote(entry.node[src]), stdout=subprocess.PIPE).stdout.decode()
            pin(p["entry"])
            issued = job(entry, dest, ident, action="entry", name=f"entry-{slot}", host=endpoint,
                      protocol=proto, native_port=port,
                      port=port, uplink=uplink, **tls)
            cfg = issued["config"]
            path = directory / f"{slot}-{proto}.{EXT[proto]}"
            atomic_write(path, cfg)
            catalog.append(dict(id=f"profile-{slot}", protocol=proto, entry=endpoint,
                                exit=exit_host.node["host"], port=port, config=cfg, devices=issued.get("devices", [])))
            print(f"{LABEL[proto]}    {endpoint}:{port} → {exit_host.node['host']}:{port}    {path}", flush=True)
        ui.stage(6, "Панель и клиентские профили")
        controller = hosts[spec["local"]]
        pin(spec["local"])
        password = job(controller, dest, ident, action="panel", name="panel", host=controller.node["host"],
                       native_port=spec["panel_native_port"],
                       panel_port=spec["panel"], catalog=catalog,
                       subscription_origin=("https://" + controller.node["tls_domain"]
                         if controller.node.get("tls_mode") == "acme" and any(
                             p["entry"] == spec["local"] and p["protocol"] == "vless" for p in profiles) else ""))["password_file"]
        spec["password_file"] = password


def show_access(spec):
    n = spec["servers"][spec["local"]]
    port = spec["panel"]
    tunnel = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-p", str(n["port"]),
              "-L", f"127.0.0.1:{port}:127.0.0.1:{port}", n["user"] + "@" + n["address"]]
    password = spec.get("password_file")
    if not password:
        # Plans saved by the first wizard release predate the access menu.
        home = Path(pwd.getpwnam("anet-vpn").pw_dir) if n["user"] == "root" else Path.home()
        password = str(home / ".local/state/anet-wizard" / spec["installation"] / "panel/node/admin-password")
    print("\n" + ui.accent("Доступ к панели") + f"  ·  {n['host']}")
    print(f"1. На VPS в сессии пользователя {n['user']} прочитайте начальный пароль admin:")
    print("   " + shlex.join(["cat", "--", password]))
    print("2. На своём компьютере откройте SSH-туннель и оставьте окно терминала открытым:")
    print("   " + shlex.join(tunnel))
    print(f"3. В браузере компьютера откройте http://127.0.0.1:{port}/ и войдите как admin.")
    print("4. В панели откройте «Профили и маршруты» → «Скачать профиль».")
    print("Чтобы снова показать эти инструкции, выполните на этом VPS:")
    print("   " + shlex.join([str(HERE.parent / "install.sh"), "--access"]))


def open_saved():
    plans = sorted(STATE.glob("*/plan.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not plans:
        raise WizardError("У текущего пользователя ещё нет завершённой установки мастера.")
    saved = [json.loads(path.read_text()) for path in plans]
    selected = 0
    if len(saved) > 1:
        choices = [(i + 1, spec["servers"][spec["local"]]["host"], spec["installation"])
                   for i, spec in enumerate(saved)]
        selected = ui.menu("Выберите панель", choices, 1) - 1
    show_access(saved[selected])


def edit_plan(spec, pending, failed_node_index=None):
    return wizard_edit.edit_plan(spec, pending, failed_node_index, ui=ui, ask=ask, number=number)


def saved_paths():
    pending = STATE / "pending.json"
    paths = sorted([*STATE.glob("*/plan.json"), *STATE.glob("*/unfinished-plan.json")],
                   key=lambda path: path.stat().st_mtime, reverse=True)
    return ([pending] if pending.is_file() else []) + paths


@contextmanager
def locked_state():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.stat().st_mode & 0o077 or STATE.stat().st_uid != os.getuid():
        raise ValueError("Каталог мастера должен принадлежать текущему пользователю и иметь права 0700")
    with (STATE / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def edit_saved():
    with locked_state():
        paths = saved_paths()
        candidates = [normalize(json.loads(path.read_text())) for path in paths]
        titles = {"pending.json": "текущий черновик", "plan.json": "установлен", "unfinished-plan.json": "архив"}
        choices = [(i + 1, spec["servers"][spec["local"]]["host"],
                    f"{titles[paths[i].name]} · {spec['installation']}") for i, spec in enumerate(candidates)]
        choices.append((0, "В главное меню", ""))
        selected = ui.menu("Какой план редактировать?", choices, 1)
        if not selected:
            return
        pending = STATE / "pending.json"
        if pending.is_file() and candidates[selected - 1]["installation"] != candidates[0]["installation"]:
            print("При сохранении выбранный план станет текущим, предыдущий черновик будет доступен в архиве.")
        if edit_plan(candidates[selected - 1], pending):
            print("План сохранён. Выберите «Установить» → «Продолжить» для применения.")


def confirm_plan(spec):
    normalize(spec)
    try:
        validate(spec, for_apply=True)
    except ValueError as error:
        print(error)
        return False
    ui.stage(2, "Проверка плана")
    profiles = active_profiles(spec)
    enabled = {p["entry"] for p in profiles if p["protocol"] == "vless"}
    sites = [node for i, node in enumerate(spec["servers"]) if i in enabled and node.get("tls_mode") == "acme"]
    disabled = [p for p in spec["profiles"] if p not in profiles]
    if disabled:
        print("Исключены из этого запуска и списка выдачи панели. Ранее созданные сервисы продолжают работу:")
        for p in disabled:
            print("  " + wizard_edit.describe(p, spec))
    print("Маршруты к установке:")
    for profile in profiles:
        entry = spec["servers"][profile["entry"]]
        exit_node = spec["servers"][profile["exit"]]
        proto = profile["protocol"]
        endpoint = entry.get("tls_domain", entry["host"]) if proto == "vless" else entry["host"]
        transport = "tcp" if proto == "vless" else "udp"
        port = spec["base"] + profile["slot"] - 1
        print(f"{LABEL[proto]}  {endpoint}:{port}/{transport} → {exit_node['host']}:{port}/udp")
    for node in sites:
        template = {"studio": "Студия", "journal": "Журнал", "github": "GitHub · " + node.get("github", "")}
        print(f"Сайт https://{node['tls_domain']}/ · nginx на TCP 443 · шаблон {template[node['template']]}.")
        print(f"Let's Encrypt использует TCP 80 для проверки домена {node['tls_domain']}. Мастер настроит продление.")
    print(f"Панель на {spec['servers'][spec['local']]['host']}, SSH-туннель к порту {spec['panel']}.")
    return ui.menu("Применить этот план?", [(1, "Начать установку", "подключиться к VPS и применить указанные изменения"),
                                       (0, "Выйти", "")], 0 if sites else 1, new_screen=False) == 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--plain", action="store_true", help="текстовый вывод без цвета и анимации")
    parser.add_argument("--access", action="store_true", help="показать доступ к установленной панели")
    args = parser.parse_args()
    ui.plain = args.plain
    ui.title()
    if args.access:
        return open_saved()
    choices = [(1, "Установить", "новая установка или продолжение сохранённой"),
               (2, "Схема и этапы установки", "что понадобится и как пройдёт настройка")]
    if next(STATE.glob("*/plan.json"), None):
        choices.append((3, "Открыть панель", "команда SSH и путь к паролю"))
    if next(STATE.glob("*/plan.json"), None) and not (STATE / "pending.json").exists():
        choices.append((5, "Обновить установленную сеть", "применить этот выпуск к сохранённой установке"))
    if saved_paths():
        choices.append((4, "Редактировать сохранённый план", "исправить доступ или изменить состав VPS"))
    choices.append((0, "Выйти", ""))
    while True:
        choice = ui.menu("Главное меню", choices, 1)
        if choice == 0:
            return
        if choice in (1, 5):
            break
        if choice == 3:
            return open_saved()
        if choice == 4:
            edit_saved()
            continue
        ui.guide()
    ui.stage(1, "Поставка и топология")
    default = args.bundle or os.environ.get("ANET_BUNDLE_DIR", "")
    if not default:
        default = ask("Каталог готовых образов (manifest.json и images.tar)", check=lambda p: Path(p).expanduser().is_dir())
    bundle = Path(default).expanduser().resolve()
    if not (bundle / "manifest.json").is_file() or not (bundle / "images.tar").is_file():
        raise ValueError("Комплект должен содержать manifest.json и images.tar")
    with locked_state():
        pending = STATE / "pending.json"
        ui.pending = pending
        if choice == 5:
            if pending.exists():
                raise WizardError("Появился незавершённый план. Откройте его через «Установить» → «Продолжить».")
            paths = sorted(STATE.glob("*/plan.json"))
            saved = [normalize(json.loads(p.read_text())) for p in paths]
            rows = [(i + 1, item["servers"][item["local"]]["host"], item["installation"]) for i, item in enumerate(saved)]
            selected = ui.menu("Выберите сеть для обновления", rows + [(0, "Вернуться без обновления", "")], 1)
            if not selected:
                return
            spec = saved[selected - 1]
        elif pending.exists():
            previous = normalize(json.loads(pending.read_text()))
            resume = ui.menu("Найдена незавершённая установка", [
                (1, "Продолжить", "использовать сохранённые узлы, профили и ключи"),
                (2, "Создать новый план", "сохранить предыдущий отдельно"),
                (3, "Редактировать", "исправить, пропустить или удалить VPS")
            ], 1)
            if resume in (1, 3):
                spec = previous
                if resume == 3 and not edit_plan(spec, pending):
                    return
            else:
                previous_dir = STATE / previous["installation"]
                previous_dir.mkdir(mode=0o700, exist_ok=True)
                archived = previous_dir / "unfinished-plan.json"
                wizard_edit.snapshot(archived, previous_dir)
                pending.rename(archived)
                spec = plan()
        else:
            spec = plan()
        if not confirm_plan(spec):
            return
        atomic_write(pending, json.dumps(spec))
        ui.resume = resume_commands(spec, bundle)
        if not recovery_apply(spec, bundle, pending, apply, edit_plan, confirm_plan, ui):
            return
        atomic_write(pending, json.dumps(spec))
        installed = STATE / spec["installation"] / "plan.json"
        wizard_edit.snapshot(installed, installed.parent)
        pending.rename(installed)
        show_access(spec)


if __name__ == "__main__":
    try:
        with credential_session():
            main()
    except KeyboardInterrupt:
        ui.error("Операция отменена оператором.")
        sys.exit(130)
    except EOFError:
        ui.error("Поток ввода закрыт. Запустите мастер из интерактивного терминала.")
        sys.exit(1)
    except BlockingIOError:
        ui.error("На этом VPS уже работает мастер. Дождитесь его завершения.")
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        ui.error(failure_hint(error.stderr or b"", "Проверьте доступ по SSH, свободное место и права на файлы."))
        sys.exit(1)
    except (OSError, ValueError, WizardError) as error:
        ui.error(str(error))
        sys.exit(1)
