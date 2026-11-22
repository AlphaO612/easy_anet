"""Upgrade the UI and its companion services in an existing wizard Compose project."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import hashlib

from bundle import load_bundle
from topology import atomic_write

ROOT = Path.home() / ".local/state/anet-wizard"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def select():
    choices = []
    for path in sorted(ROOT.glob("*/panel/webui-mount.json")):
        directory = path.parent
        ident = directory.parent.name
        if not re.fullmatch(r"[0-9a-f]{10}", ident):
            continue
        project = f"anet-{ident}-panel"
        cid = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
                     "--filter", "label=com.docker.compose.service=webui").splitlines()
        if len(cid) == 1:
            choices.append((directory, project, cid[0]))
    if not choices:
        raise ValueError("Панель мастера не найдена у текущего пользователя Docker. Проверьте учётную запись установки.")
    for index, (directory, project, _) in enumerate(choices, 1):
        node = json.loads((directory / "node/node.json").read_text())
        print(f"{index}. {node['host']}   {project}")
    choice = input("Какую панель обновить? [1]: ").strip() or "1"
    if not choice.isdigit() or not 1 <= int(choice) <= len(choices):
        raise ValueError("Неверный номер панели.")
    return choices[int(choice) - 1]


def upgrade(directory, project, cid):
    binding = json.loads(docker("inspect", "--format", "{{json .HostConfig.PortBindings}}", cid))["8080/tcp"]
    if len(binding) != 1 or binding[0]["HostIp"] != "127.0.0.1":
        raise ValueError("Ожидается штатная панель с привязкой к 127.0.0.1.")
    root = directory.parent
    refs_path = root / "images.json"
    refs = json.loads(refs_path.read_text())
    catalog = directory / "vless-catalog"
    catalog.mkdir(exist_ok=True)
    catalog.chmod(0o755)
    overlay_path = directory / "webui-mount.json"
    overlay = json.loads(overlay_path.read_text())
    overlay["services"]["vless-profiles"] = {"volumes": [{
        "type": "bind", "source": str(catalog), "target": "/srv/anet-vless-catalog", "read_only": True}]}
    print("Проверяю и загружаю образ панели.")
    new_refs = load_bundle(Path("bundle"), "panel-update")
    env = dict(os.environ, **{**refs, **new_refs}, STATE_DIR=str(directory / "node"),
               PANEL_PORT=binding[0]["HostPort"], PANEL_BIND="127.0.0.1", PROBE_VOLUME=project+"-probes")
    version = hashlib.sha256(Path("bundle/manifest.json").read_bytes()).hexdigest()[:16]
    model = directory / "updates" / version
    model.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "compose", "--env-file", "/dev/null", "-p", project]
    for name in ("compose.yaml", "webui.yaml", "probe-management.yaml"):
        target = model / name
        atomic_write(target, (Path("compose") / name).read_text())
        cmd += ["-f", str(target)]
    candidate = model / "webui-mount.json"
    atomic_write(candidate, json.dumps(overlay))
    cmd += ["-f", str(candidate)]
    subprocess.run([*cmd, "config", "--quiet"], env=env, check=True)
    print("Перезапускаю интерфейс и службы регистрации VPS и выдачи профилей.", flush=True)
    services = ("node-install", "vless-profiles", "webui")
    up = [*cmd, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "--wait", "--wait-timeout", "120"]
    previous = [name for name in services if docker("ps", "-q", "--filter", "label=com.docker.compose.project="+project,
                                                    "--filter", "label=com.docker.compose.service="+name)]
    try:
        subprocess.run([*up, *services], env=env, check=True)
    except subprocess.CalledProcessError:
        print("Возвращаю прежний образ ранее работавших служб.", file=sys.stderr)
        old_env = {**env, **refs}
        for name in services:
            if name not in previous:
                subprocess.run([*cmd, "stop", name], env=env)
        if previous:
            subprocess.run([*up, *previous], env=old_env, check=True)
        raise
    atomic_write(overlay_path, json.dumps(overlay))
    atomic_write(refs_path, json.dumps({**refs, **new_refs}))
    print("Панель обновлена. Откройте её по обычному SSH-туннелю и обновите страницу браузера.")


if __name__ == "__main__":
    try:
        if "name=rootless" not in docker("info", "--format", "{{json .SecurityOptions}}"):
            raise ValueError("Требуется rootless Docker владельца установки.")
        directory, project, cid = select()
        if input("Применить обновление панели? [д/Н]: ").lower() in ("д", "да", "y", "yes"):
            with (directory.parent / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                upgrade(directory, project, cid)
    except BlockingIOError:
        sys.exit("Установщик уже применяет изменения. Дождитесь его завершения.")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Обновление панели прервано. Проверьте Docker и повторите команду. {error}")
