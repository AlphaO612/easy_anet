"""Issue pre-authorized Xray devices and serve a scoped subscription per recipient."""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import quote, urlsplit
import uuid

DB = Path("/var/lib/anet-vless/profiles.sqlite")
CATALOG = Path("/srv/anet-vless-catalog/index.json")
ADMIN = "/api/v1/vless-devices"
PUBLIC = "/api/v1/vless-subscriptions/"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def read_catalog():
    try:
        return json.loads(CATALOG.read_text())
    except FileNotFoundError:
        return {"origin": "", "routes": {}}


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init():
    os.umask(0o077)
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS recipients (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, token_hash TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS devices (
                credential_hash TEXT PRIMARY KEY, recipient TEXT NOT NULL, route TEXT NOT NULL, name TEXT NOT NULL,
                UNIQUE(recipient, route, name)
            );
        """)


def text_field(body, field):
    value = body.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise Problem(400, "Введите название длиной до 80 символов.")
    return value.strip()


def device_rows(db, group, catalog):
    routes = catalog["routes"]
    # Match using a one-way identifier so taking a database backup does not disclose VPN credentials.
    current = {route: {digest(key): key for key in data["devices"]} for route, data in routes.items()}
    result = []
    for row in db.execute("SELECT name, route, credential_hash FROM devices WHERE recipient=? ORDER BY name, route", (group,)):
        route = routes.get(row["route"])
        key = current.get(row["route"], {}).get(row["credential_hash"])
        uri = None
        if route and key:
            host, port = route["entry"], route["port"]
            uri = f"vless://{key}@{host}:{port}?encryption=none&security=tls&type=tcp&sni={host}#{quote(row['name'], safe='')}"
        result.append({"name": row["name"], "route": row["route"], "uri": uri})
    return result


def dispatch(db, method, path, body):
    catalog = read_catalog()
    if path.startswith(PUBLIC):
        token = path[len(PUBLIC):]
        if method != "GET" or not TOKEN.fullmatch(token):
            raise Problem(404, "Ссылка недействительна.")
        group = db.execute("SELECT id FROM recipients WHERE token_hash=?", (digest(token),)).fetchone()
        if not group:
            raise Problem(404, "Ссылка недействительна.")
        configs = [d["uri"] for d in device_rows(db, group["id"], catalog) if d["uri"]]
        if not configs:
            raise Problem(503, "Для подписки нет действующих профилей.")
        return base64.b64encode(("\n".join(configs) + "\n").encode())
    if path == ADMIN:
        if method == "GET":
            routes = [{"id": k, "entry": r["entry"], "exit": r["exit"], "port": r["port"]}
                      for k, r in catalog["routes"].items()]
            groups = [dict(row) for row in db.execute(
                "SELECT id, name, token_hash IS NOT NULL AS subscribed FROM recipients ORDER BY name")]
            return {"groups": groups, "routes": routes, "origin": catalog.get("origin", "")}
        if method == "POST":
            name = text_field(body, "name")
            group = uuid.uuid4().hex
            try:
                db.execute("INSERT INTO recipients(id,name) VALUES(?,?)", (group, name))
            except sqlite3.IntegrityError:
                raise Problem(409, "Получатель с таким именем уже существует.") from None
            return {"id": group}
    match = re.fullmatch(re.escape(ADMIN) + r"/([a-f0-9]{32})/(profiles|token)", path)
    if not match:
        raise Problem(404, "Адрес не найден.")
    group, action = match.groups()
    if method != "GET":
        db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT id FROM recipients WHERE id=?", (group,)).fetchone():
        raise Problem(404, "Получатель не найден.")
    if action == "profiles":
        if method == "GET":
            return device_rows(db, group, catalog)
        if method == "POST":
            name = text_field(body, "name")
            route = body.get("route")
            if not isinstance(route, str) or route not in catalog["routes"]:
                raise Problem(400, "Выберите действующий VLESS-вход.")
            existing = db.execute("SELECT 1 FROM devices WHERE recipient=? AND route=? AND name=?", (group, route, name)).fetchone()
            if not existing:
                used = {row[0] for row in db.execute("SELECT credential_hash FROM devices")}
                key = next((digest(k) for k in catalog["routes"][route]["devices"] if digest(k) not in used), None)
                if key is None:
                    raise Problem(409, "На маршруте уже выданы все 128 профилей. Выберите другой вход.")
                db.execute("INSERT INTO devices VALUES(?,?,?,?)", (key, group, route, name))
            return device_rows(db, group, catalog)
    if action == "token":
        if method == "POST":
            if not any(d["uri"] for d in device_rows(db, group, catalog)):
                raise Problem(409, "Сначала создайте профиль на действующем маршруте.")
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE recipients SET token_hash=? WHERE id=?", (digest(token), group))
            return {"path": PUBLIC + token}
        if method == "DELETE":
            db.execute("UPDATE recipients SET token_hash=NULL WHERE id=?", (group,))
            return {}
    raise Problem(405, "Действие не поддерживается.")


def authenticate(header):
    if not header.startswith("Bearer ") or len(header) > 8192:
        raise Problem(401, "Войдите в панель.")
    connection = http.client.HTTPConnection("auth", 3000, timeout=8)
    try:
        connection.request("GET", "/api/v1/servers", headers={"Authorization": header})
        status = connection.getresponse().status
        if status != 200:
            raise Problem(401 if status in (401, 403) else 503, "Не удалось подтвердить сеанс администратора.")
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    # Both error and access log lines would otherwise include the subscription bearer.
    def log_message(self, *_):
        pass

    def handle_request(self):
        try:
            path = urlsplit(self.path).path
            if path == "/ready" and self.command == "GET" and self.client_address[0] == "127.0.0.1":
                db = connect()
                try:
                    db.execute("SELECT id FROM recipients LIMIT 1").fetchone()
                finally:
                    db.close()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not path.startswith(PUBLIC):
                authenticate(self.headers.get("Authorization", ""))
            body = {}
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 2048:
                    raise Problem(413, "Слишком большой запрос.")
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError()
            db = connect()
            try:
                with db:
                    result = dispatch(db, self.command, path, body)
            finally:
                db.close()
            status = 200
        except Problem as error:
            status, result = error.status, {"error": error.message}
        except (ValueError, UnicodeError):
            status, result = 400, {"error": "Неверный формат запроса."}
        except (OSError, sqlite3.Error, http.client.HTTPException):
            status, result = 503, {"error": "Хранилище профилей или сервис авторизации недоступен."}
        binary = isinstance(result, bytes)
        data = result if binary else json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8" if binary else "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = handle_request


if __name__ == "__main__":
    init()
    ThreadingHTTPServer(("0.0.0.0", 8093), Handler).serve_forever()
