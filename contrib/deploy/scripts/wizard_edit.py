"""Edit local deployment plans without contacting hosts or changing running services."""
import copy
import json
from pathlib import Path
import re
import secrets

from topology import atomic_write


STATES = {"active": "включён", "skipped": "пропущен", "removed": "удалён"}
ROLES = {"entry": "вход", "exit": "выход"}


def hostname(value):
    return bool(isinstance(value, str) and len(value) <= 253 and all(
        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", x)
        for x in value.split(".")))


def normalize(spec):
    """Add stable identities to older plans, preserving their original allocations."""
    try:
        servers = spec["servers"]
        profiles = spec["profiles"]
        if not isinstance(servers, list) or not isinstance(profiles, list):
            raise ValueError("Ожидаются списки узлов и профилей.")
        for i, node in enumerate(servers):
            node.setdefault("status", "active")
            if "role" not in node:
                sides = {side for p in profiles for side in ROLES if p[side] == i}
                if len(sides) != 1:
                    raise ValueError(f"Невозможно определить роль узла {i + 1}.")
                node["role"] = sides.pop()
        for i, profile in enumerate(profiles):
            profile.setdefault("slot", i + 1)
        if "panel_native_port" not in spec:
            base = spec["base"]
            spec["panel_native_port"] = 44000 if base > 44000 or base + len(profiles) <= 44000 else 45000
    except (AttributeError, TypeError, KeyError) as error:
        raise ValueError("Повреждена структура сохранённого плана.") from error
    validate(spec, check_ssh_duplicates=False)
    return spec


def active_profiles(spec):
    nodes = spec["servers"]
    return [p for p in spec["profiles"]
            if nodes[p["entry"]]["status"] == nodes[p["exit"]]["status"] == "active"]


def validate(spec, *, for_apply=False, check_ssh_duplicates=True):
    nodes = spec["servers"]
    local = spec.get("local")
    if not re.fullmatch(r"[a-f0-9]{10}", str(spec.get("installation", ""))):
        raise ValueError("Неверный идентификатор установки.")
    if type(local) is not int or not 0 <= local < len(nodes):
        raise ValueError("В плане отсутствует VPS панели.")
    for key in ("base", "panel", "panel_native_port"):
        value = spec.get(key)
        if type(value) is not int or not 1024 <= value <= 65535:
            raise ValueError(f"Некорректный порт {key}.")
    addresses = set()
    for index, node in enumerate(nodes):
        if node["role"] not in ROLES or node["status"] not in STATES:
            raise ValueError(f"Неверные роль или статус узла {index + 1}.")
        if node.get("local") is not (index == local) or index == local and node["status"] != "active":
            raise ValueError("В плане должен быть один активный локальный VPS панели.")
        if not hostname(node.get("host")) or not hostname(node.get("address")):
            raise ValueError(f"Неверный адрес узла {index + 1}.")
        if type(node.get("port")) is not int or not 1 <= node["port"] <= 65535:
            raise ValueError(f"Неверный порт SSH узла {index + 1}.")
        endpoint = (node["address"].lower(), node["port"])
        if node["status"] == "active":
            if endpoint in addresses and check_ssh_duplicates:
                raise ValueError(f"Несколько активных VPS имеют один SSH-адрес {node['address']}:{node['port']}.")
            addresses.add(endpoint)
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", str(node.get("user", ""))):
            raise ValueError(f"Неверное имя пользователя узла {index + 1}.")
    slots = set()
    for p in spec["profiles"]:
        slot = p.get("slot")
        if type(slot) is not int or slot <= 0 or slot in slots or spec["base"] + slot - 1 > 65535:
            raise ValueError("Номер профиля повторяется или выходит за диапазон портов.")
        slots.add(slot)
        if p.get("protocol") not in ("anet", "wireguard", "vless"):
            raise ValueError(f"Неверный протокол профиля {slot}.")
        for side in ROLES:
            idx = p.get(side)
            if type(idx) is not int or not 0 <= idx < len(nodes) or nodes[idx]["role"] != side:
                raise ValueError(f"Профиль {slot} ссылается на неизвестный или неподходящий узел.")
    if for_apply and not active_profiles(spec):
        raise ValueError("Нет действующего маршрута. Верните пропущенный узел или создайте новый план.")


def describe(p, spec):
    nodes = spec["servers"]
    entry = nodes[p["entry"]]
    endpoint = entry.get("tls_domain", entry["host"]) if p["protocol"] == "vless" else entry["host"]
    return (f"#{p['slot']} · {p['protocol']} · {endpoint}:{spec['base'] + p['slot'] - 1}"
            f" → {nodes[p['exit']]['host']}")


def snapshot(path, directory):
    if path.is_file():
        history = directory / "plan-history"
        history.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(history / (secrets.token_hex(8) + ".json"), path.read_text())


def save_draft(path, draft):
    if path.exists():
        previous = normalize(json.loads(path.read_text()))
        archive = path.parent / previous["installation"]
        snapshot(path, archive)
        if previous["installation"] != draft["installation"]:
            old = archive / "unfinished-plan.json"
            snapshot(old, archive)
            atomic_write(old, path.read_text())
    atomic_write(path, json.dumps(draft, ensure_ascii=False))


def update_connection(node, ask, number, menu):
    print("Изменение SSH-адреса сохраняет назначения. Если VPN уже запущен, подключайтесь к тому же VPS "
          "и пользователю rootless Docker: выданные клиентам профили содержат прежний публичный адрес.")
    node["host"] = ask("Публичный адрес VPN", node["host"], hostname)
    node["address"] = ask("Адрес SSH", node["address"], hostname)
    node["port"] = number("Порт SSH", node["port"], 1, 65535)
    if node["local"]:
        return
    node["user"] = ask("Пользователь SSH", node["user"], lambda v: re.fullmatch(r"[a-z_][a-z0-9_-]*", v))
    old = 2 if node.get("password") else 1
    node["password"] = menu("Авторизация SSH", [(1, "Ключ или агент SSH", ""), (2, "Пароль", "")], old) == 2
    if node["password"]:
        node.pop("key", None)
        print("Пароль будет запрошен перед следующим подключением.")
    else:
        key = ask("Файл ключа (знак минус — стандартный ключ или агент)", node.get("key") or "-",
                  lambda p: p == "-" or Path(p).expanduser().is_file())
        node["key"] = "" if key == "-" else key


def edit_plan(spec, pending, failed_node_index=None, *, ui, ask, number):
    draft = normalize(copy.deepcopy(spec))
    selected = failed_node_index
    while True:
        nodes = draft["servers"]
        if selected is None:
            options = [(i + 1, f"{ROLES[n['role']]} · {n['host']}", STATES[n["status"]] +
                        (" · здесь панель" if n["local"] else "")) for i, n in enumerate(nodes)
                       if n["status"] != "removed"]
            save_id = len(nodes) + 1
            options.extend([(save_id, "Сохранить план", "применение подтверждается отдельно"),
                            (0, "Отменить правки", "вернуться без сохранения")])
            choice = ui.menu("Редактор плана · выберите VPS", options, save_id)
            if choice == 0:
                return False
            if choice == save_id:
                try:
                    validate(draft)
                except ValueError as error:
                    ui.menu(str(error), [(0, "Вернуться к редактированию", "")], 0)
                    continue
                save_draft(pending, draft)
                spec.clear()
                spec.update(draft)
                if not active_profiles(draft):
                    print("Черновик сохранён. Для запуска потребуется действующий маршрут.")
                return True
            selected = choice - 1
        node = nodes[selected]
        affected = [p for p in draft["profiles"] if selected in (p["entry"], p["exit"])]
        print(f"\n{ROLES[node['role']]} · {node['host']} · SSH {node['user']}@{node['address']}:{node['port']}")
        print("Затронутые профили:")
        for p in affected:
            print("  " + describe(p, draft))
        options = [(1, "Исправить адрес и доступ", "SSH и адрес VPN")]
        if not node["local"]:
            options.append((2, "Вернуть в установку" if node["status"] == "skipped" else "Временно пропустить",
                            "маршруты этого VPS участвуют только когда оба конца включены"))
            options.append((3, "Удалить из плана", "исключить перечисленные назначения из будущего запуска"))
        options.append((0, "К списку VPS", ""))
        action = ui.menu("Действие с VPS", options, 0, new_screen=False)
        if action == 1:
            update_connection(node, ask, number, ui.menu)
        elif action == 2:
            node["status"] = "active" if node["status"] == "skipped" else "skipped"
        elif action == 3:
            approved = ui.menu("Убрать VPS и его назначения из плана? Уже запущенные VPN продолжат работу.",
                               [(1, "Удалить из плана", node["host"]), (0, "Оставить", "")], 0)
            if approved:
                node["status"] = "removed"
        selected = None
