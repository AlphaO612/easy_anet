"""Collect credentials in memory and recover from a failed SSH preflight."""
from contextlib import contextmanager
from contextvars import ContextVar
import getpass
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import warnings

from wizard_ui import WizardError

_credentials = ContextVar("wizard_credentials", default=None)


@contextmanager
def credential_session():
    passwords = {}
    token = _credentials.set(passwords)
    try:
        yield passwords
    finally:
        passwords.clear()
        _credentials.reset(token)


def prompt_for_new_node(node):
    passwords = _credentials.get()
    if passwords is not None and sys.stdin.isatty():
        collect_passwords([node], passwords)


def identity(node):
    return node["user"], node["address"], node["port"], node.get("key"), node.get("password", False)


def collect_passwords(nodes, passwords):
    for node in nodes:
        if node["local"] or identity(node) in passwords:
            continue
        prompt = f"SSH-пароль для {node['user']}@{node['address']}:{node['port']} (Enter — вход по ключу): "
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(prompt)
            except getpass.GetPassWarning:
                raise WizardError("Для скрытого ввода пароля откройте мастер через ssh -tt.") from None
        passwords[identity(node)] = value


def reason(diagnostic):
    text = diagnostic.lower()
    if b"banner exchange" in text:
        return "SSH не получил приветствие сервера до тайм-аута. Пароль ещё не проверялся."
    if b"permission denied" in text:
        return "Сервер отклонил авторизацию. Проверьте пользователя, пароль или ключ."
    if b"host key verification failed" in text or b"host identification has changed" in text:
        return "Не удалось подтвердить ключ сервера. Сверьте отпечаток в консоли провайдера."
    if b"resolve hostname" in text:
        return "Не удалось разрешить имя SSH-сервера. Проверьте DNS или укажите IP."
    if b"connection refused" in text:
        return "Сервер отказал в соединении. Проверьте порт и службу SSH."
    if b"timed out" in text or b"no route to host" in text:
        return "Сервер недоступен с первой VPS. Проверьте адрес, порт и firewall."
    return "Не удалось установить SSH-соединение. Проверьте адрес, порт и способ входа."


class NodeConnectionError(WizardError):
    def __init__(self, node, diagnostic):
        super().__init__(reason(diagnostic))
        self.node = node
        self.auth_failed = b"permission denied" in diagnostic.lower()


@contextmanager
def password_environment(password):
    if not password:
        yield os.environ.copy()
        return
    with tempfile.TemporaryDirectory(prefix="anet-pass-") as tmp, socket.socket(socket.AF_UNIX) as listener:
        address = str(Path(tmp) / "ask")
        listener.bind(address)
        listener.listen(1)
        listener.settimeout(0.15)
        stop = Event()

        def serve():
            while not stop.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    try:
                        client.sendall(password.encode() + b"\n")
                    except BrokenPipeError:
                        pass

        worker = Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield dict(os.environ, SSH_ASKPASS=str(Path(__file__).with_name("wizard_askpass.py")),
                       SSH_ASKPASS_REQUIRE="force", ANET_ASKPASS_SOCKET=address)
        finally:
            stop.set()
            worker.join()


def connect(node, options, target):
    passwords = _credentials.get() or {}
    password = passwords.get(identity(node))
    auth = []
    if password:
        methods = "password,keyboard-interactive" if node.get("password") else "publickey,password,keyboard-interactive"
        auth = ["-o", "PreferredAuthentications=" + methods]
    with tempfile.NamedTemporaryFile() as log, password_environment(password) as env:
        result = subprocess.run(["ssh", *auth, *options[1:], "-E", log.name, "-o", "NumberOfPasswordPrompts=1",
                                 "-M", "-fN", target], env=dict(env, LC_ALL="C"))
        if result.returncode:
            log.seek(max(0, os.fstat(log.fileno()).st_size - 8192))
            raise NodeConnectionError(node, log.read())


def resume_commands(spec, bundle):
    launcher = Path(bundle) / "install.sh"
    args = ["bash", str(launcher)] if launcher.is_file() else [
        "env", "ANET_BUNDLE_DIR=" + str(bundle), "bash", str(Path(__file__).resolve().parents[1] / "install.sh")]
    remote = shlex.join(args)
    local = spec["servers"][spec["local"]]
    login = shlex.join(["ssh", "-tt", "-p", str(local["port"]), local["user"] + "@" + local["address"], remote])
    return f"На первой VPS:\n  {remote}\nИли с компьютера:\n  {login}"


def recovery_apply(spec, bundle, pending, apply, edit_plan, confirm_plan, ui):
    from wizard_edit import active_profiles
    passwords = _credentials.get()
    if passwords is None:
        with credential_session():
            return recovery_apply(spec, bundle, pending, apply, edit_plan, confirm_plan, ui)
    while True:
        rows = active_profiles(spec)
        used = {spec["local"]}
        for row in rows:
            used.update((row["entry"], row["exit"]))
        collect_passwords([spec["servers"][i] for i in sorted(used)], passwords)
        try:
            apply(spec, bundle)
            return True
        except NodeConnectionError as error:
            node = error.node
            if error.auth_failed:
                passwords.pop(identity(node), None)
            ui.screen("Соединение с VPS прервано", str(error))
            check = ["ssh", "-v", "-o", "ConnectTimeout=15", "-p", str(node["port"])]
            if node.get("key"):
                check += ["-i", node["key"]]
            print(f"Проверка с первой VPS: {shlex.join(check + [node['user'] + '@' + node['address']])}")
            print("Если SSH зависает до запроса пароля, проверьте адрес, порт, firewall и sshd в консоли провайдера.")
            print("План сохранён. Чтобы продолжить после выхода:")
            print(resume_commands(spec, bundle))
            while True:
                choice = ui.menu("Что сделать сейчас?", [
                    (1, "Повторить подключение", node["address"]),
                    (2, "Настроить этот сервер", "исправить адрес или вход, пропустить или удалить из плана"),
                    (0, "Сохранить и выйти", "вернуться к этому плану позже")], 2, new_screen=False)
                if choice == 0:
                    return False
                if choice == 1:
                    break
                if edit_plan(spec, pending, spec["servers"].index(node)):
                    if not confirm_plan(spec):
                        return False
                    break
