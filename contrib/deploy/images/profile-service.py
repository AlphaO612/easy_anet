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
import time
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
        catalog = json.loads(CATALOG.read_text())
    except FileNotFoundError:
        catalog = {"origin": "", "routes": {}}
    try:
        # A separately exposed subscription host also works when the panel is on an exit VPS.
        explicit = CATALOG.with_name("subscription-origin").read_text().strip()
        if explicit:
            catalog["origin"] = explicit
    except FileNotFoundError:
        pass
    return catalog


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
            CREATE TABLE IF NOT EXISTS account_links (
                user_id TEXT PRIMARY KEY, recipient TEXT NOT NULL UNIQUE REFERENCES recipients(id)
            );
            CREATE TABLE IF NOT EXISTS traffic (
                credential TEXT NOT NULL, source TEXT NOT NULL, upload INTEGER NOT NULL, download INTEGER NOT NULL,
                PRIMARY KEY(credential,source)
            );
            CREATE TABLE IF NOT EXISTS traffic_reports (catalog TEXT PRIMARY KEY, received INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (
                credential_hash TEXT PRIMARY KEY, recipient TEXT NOT NULL, route TEXT NOT NULL, name TEXT NOT NULL,
                UNIQUE(recipient, route, name)
            );
        """)


def report_route(route_id, token):
    route = read_catalog()["routes"].get(route_id)
    meter = route.get("usage", {}) if route else {}
    if not TOKEN.fullmatch(token) or not secrets.compare_digest(meter.get("token_hash", ""), digest(token)):
        raise Problem(403, "Ключ сбора трафика не принят.")
    return route


def record_usage(db, route, report):
    meter = route["usage"]
    if report.get("catalog") != meter["catalog"]:
        raise Problem(409, "Каталог входа изменился. Примените план в мастере.")
    source, totals = report.get("source"), report.get("totals")
    if not isinstance(source, str) or not re.fullmatch(r"[a-f0-9]{32}", source) or not isinstance(totals, dict) or len(totals) > 129:
        raise Problem(400, "Некорректный отчёт Xray.")
    keys = {str(slot): digest(key) for slot, key in enumerate(route["devices"])}
    keys["starter"] = meter["starter_hash"]
    records = []
    for slot, pair in totals.items():
        if slot not in keys or not isinstance(pair, list) or len(pair) != 2 or any(type(n) != int or not 0 <= n < 2**60 for n in pair):
            raise Problem(400, "Некорректный счётчик Xray.")
        records.append((keys[slot], source, *pair))
    db.executemany("""INSERT INTO traffic VALUES(?,?,?,?)
        ON CONFLICT(credential,source) DO UPDATE SET
        upload=max(upload,excluded.upload), download=max(download,excluded.download)""", records)
    db.execute("INSERT OR REPLACE INTO traffic_reports(catalog,received) VALUES(?,?)", (meter["catalog"], int(time.time())))
    return {}


def traffic_for(db, route, credential):
    meter = (route or {}).get("usage", {})
    seen = db.execute("SELECT received FROM traffic_reports WHERE catalog=?", (meter.get("catalog"),)).fetchone()
    if not seen:
        return None
    amounts = db.execute("SELECT upload, download FROM traffic WHERE credential=?", (credential,)).fetchall()
    return {"upload": sum(r[0] for r in amounts), "download": sum(r[1] for r in amounts), "updated": seen[0]}


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
        result.append({"name": row["name"], "route": row["route"], "uri": uri,
                       "traffic": traffic_for(db, route, row["credential_hash"])})
    return result


def account_exists(user_id):
    # The existing auth service checks is_active before returning its QR landing page.
    # Only the HTTP status is needed. No admin credential is stored for subscription reads.
    connection = http.client.HTTPConnection("auth", 3000, timeout=5)
    try:
        connection.request("GET", "/api/v1/config/qr/" + user_id)
        status = connection.getresponse().status
    finally:
        connection.close()
    if status not in (200, 404):
        raise Problem(503, "Не удалось проверить состояние пользователя.")
    return status == 200


def connect_account(db, body):
    value = body.get("user_id")
    try:
        user_id = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        user_id = None
    if not user_id or not account_exists(user_id):
        raise Problem(400, "Выберите действующего пользователя из панели.")
    legacy = body.get("recipient")
    if legacy is not None and (not isinstance(legacy, str) or not re.fullmatch(r"[a-f0-9]{32}", legacy)):
        raise Problem(400, "Выберите прежнего получателя.")
    db.execute("BEGIN IMMEDIATE")
    linked = db.execute("SELECT recipient FROM account_links WHERE user_id=?", (user_id,)).fetchone()
    if linked:
        if legacy and legacy != linked[0]:
            raise Problem(409, "У пользователя уже есть отдельный набор профилей.")
        return {"id": linked[0]}
    if legacy:
        if not db.execute("SELECT id FROM recipients WHERE id=?", (legacy,)).fetchone():
            raise Problem(404, "Прежний получатель не найден.")
        if db.execute("SELECT user_id FROM account_links WHERE recipient=?", (legacy,)).fetchone():
            raise Problem(409, "Этот набор уже привязан к пользователю.")
        group = legacy
    else:
        group = uuid.uuid4().hex
        # Older releases have a UNIQUE name column. Keep the internal key independent of the display name.
        db.execute("INSERT INTO recipients(id,name) VALUES(?,?)", (group, "\x01" + group))
    db.execute("INSERT INTO account_links(user_id, recipient) VALUES(?,?)", (user_id, group))
    return {"id": group}


def dispatch(db, method, path, body):
    catalog = read_catalog()
    if path.startswith(PUBLIC):
        token = path[len(PUBLIC):]
        if method != "GET" or not TOKEN.fullmatch(token):
            raise Problem(404, "Ссылка недействительна.")
        group = db.execute("""SELECT id, user_id FROM recipients LEFT JOIN account_links
            ON recipient=id WHERE token_hash=?""", (digest(token),)).fetchone()
        if not group or (group["user_id"] and not account_exists(group["user_id"])):
            raise Problem(404, "Ссылка недействительна.")
        configs = [d["uri"] for d in device_rows(db, group["id"], catalog) if d["uri"]]
        if not configs:
            raise Problem(503, "Для подписки нет действующих профилей.")
        return base64.b64encode(("\n".join(configs) + "\n").encode())
    if path == ADMIN:
        if method == "GET":
            routes = [{"id": k, "entry": r["entry"], "exit": r["exit"], "port": r["port"],
                       "starter_traffic": traffic_for(db, r, r.get("usage", {}).get("starter_hash"))}
                      for k, r in catalog["routes"].items()]
            groups = [dict(row) for row in db.execute(
                """SELECT id, CASE WHEN user_id IS NULL THEN name ELSE '' END AS name, user_id,
                   token_hash IS NOT NULL AS subscribed FROM recipients LEFT JOIN account_links
                   ON recipient=id ORDER BY name""")]
            return {"groups": groups, "routes": routes, "origin": catalog.get("origin", "")}
        if method == "POST":
            return connect_account(db, body)
    match = re.fullmatch(re.escape(ADMIN) + r"/([a-f0-9]{32})/(profiles|token)", path)
    if not match:
        raise Problem(404, "Адрес не найден.")
    group, action = match.groups()
    record = db.execute("""SELECT id, user_id FROM recipients LEFT JOIN account_links
        ON recipient=id WHERE id=?""", (group,)).fetchone()
    if not record:
        raise Problem(404, "Получатель не найден.")
    if method == "POST" and (not record["user_id"] or not account_exists(record["user_id"])):
        raise Problem(409, "Сначала привяжите набор к действующему пользователю.")
    if method != "GET":
        db.execute("BEGIN IMMEDIATE")
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
            report = re.fullmatch(r"/api/v1/vless-usage/(profile-[1-9][0-9]*)", path)
            if report:
                if self.command != "POST":
                    raise Problem(405, "Нужен POST.")
                route = report_route(report[1], self.headers.get("X-Usage-Token", ""))
            elif not path.startswith(PUBLIC):
                authenticate(self.headers.get("Authorization", ""))
            body = {}
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= (12288 if report else 2048):
                    raise Problem(413, "Слишком большой запрос.")
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError()
            db = connect()
            try:
                with db:
                    result = record_usage(db, route, body) if report else dispatch(db, self.command, path, body)
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
