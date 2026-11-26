#!/usr/bin/env python3
"""Internal Telegram Bot API relay over existing ANet encrypted exit tunnels.

Listens ONLY inside the rootless Docker network namespace (not exposed on host
public interfaces) and forwards Telegram Bot API POST requests through the
selected exit server's encrypted ANet uplink tunnel via SO_MARK=177.
"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import urllib.parse

SO_MARK = 36
TUNNEL_MARK = 177
TG_HOST = "api.telegram.org"

# Minimal helper executed inside the target entry-N-network-1 netns via nsenter.
_NETNS_SENDER = r"""
import http.client, json, socket, ssl, sys

SO_MARK = 36
MARK = 177
req = json.loads(sys.stdin.read())
host = req["host"]
path = req["path"]
body = req["body"].encode("utf-8")
timeout = float(req.get("timeout", 12))

def marked_connect(addr, t):
    h, p = addr
    last = None
    for res in socket.getaddrinfo(h, p, socket.AF_INET, socket.SOCK_STREAM):
        af, st, pr, _, sa = res
        s = socket.socket(af, st, pr)
        s.setsockopt(socket.SOL_SOCKET, SO_MARK, MARK)
        s.settimeout(t)
        try:
            s.connect(sa)
            return s
        except Exception as exc:
            last = exc
            s.close()
    raise last or OSError("connect failed")

class MarkedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = marked_connect((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

ctx = ssl.create_default_context()
conn = MarkedHTTPS(host, 443, timeout=timeout, context=ctx)
try:
    conn.request(
        "POST",
        path,
        body=body,
        headers={
            "Host": host,
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "User-Agent": "anet-tg-relay/1.0",
        },
    )
    resp = conn.getresponse()
    raw = resp.read(65536).decode("utf-8", "replace")
    print(json.dumps({"status": resp.status, "body": raw}))
finally:
    conn.close()
"""


def _state_root() -> Path:
    explicit = os.environ.get("ANET_STATE_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    ident = os.environ.get("ANET_INSTALL_ID", "").strip()
    base = Path.home() / ".local/state/anet-wizard"
    if ident and (base / ident).is_dir():
        return base / ident
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "panel").is_dir():
                return child
    return base


def _install_id(root: Path) -> str:
    return os.environ.get("ANET_INSTALL_ID", "").strip() or root.name


def _parse_exit_host(uplink_toml: Path) -> str | None:
    try:
        text = uplink_toml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^\s*dsn\s*=\s*"[^":/]+://(?:\[([^\]]+)\]|([^:/"]+)):\d+', text, re.M)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    m2 = re.search(r'^\s*host\s*=\s*"([^"]+)"', text, re.M)
    return m2.group(1).strip() if m2 else None


def discover_tunnels() -> list[dict[str, str]]:
    """Return list of running local exit tunnels: [{slot, route_id, exit_host, container, pid}]."""
    root = _state_root()
    ident = _install_id(root)
    if not root.is_dir():
        return []
    tunnels: list[dict[str, str]] = []
    for entry_dir in sorted(root.glob("entry-*")):
        uplink = entry_dir / "uplink.toml"
        if not uplink.is_file():
            continue
        exit_host = _parse_exit_host(uplink)
        if not exit_host:
            continue
        slot_name = entry_dir.name  # e.g. entry-2
        slot_num = slot_name.split("-", 1)[-1]
        cname = f"anet-{ident}-{slot_name}-network-1"
        try:
            pid = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.Pid}}", cname],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=4,
            ).strip()
        except Exception:
            continue
        if not pid or pid == "0":
            continue
        tunnels.append(
            {
                "slot": slot_name,
                "route_id": f"profile-{slot_num}",
                "exit_host": exit_host,
                "container": cname,
                "pid": pid,
            }
        )
    return tunnels


def forward_via_pid(pid: str, tg_path: str, body_text: str) -> tuple[int, str]:
    payload = json.dumps({"host": TG_HOST, "path": tg_path, "body": body_text, "timeout": 12})
    # When already running inside rootlesskit's user namespace, only `-n` is needed;
    # fall back to `-U -n --preserve-credentials` if invoked from the host userns.
    cmd_variants = [
        ["nsenter", "-t", pid, "-n", sys.executable, "-c", _NETNS_SENDER],
        ["nsenter", "-t", pid, "-U", "-n", "--preserve-credentials", sys.executable, "-c", _NETNS_SENDER],
    ]
    last_err = "nsenter failed"
    for cmd in cmd_variants:
        proc = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            timeout=16,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = json.loads(proc.stdout.strip())
            return int(parsed["status"]), str(parsed["body"])
        last_err = (proc.stderr or proc.stdout or "nsenter failed").strip()
    raise RuntimeError(last_err[:300])


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return  # Keep logs quiet and never print bot tokens in URLs

    def _send_json(self, code: int, obj: object, extra_headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            tunnels = discover_tunnels()
            exits = sorted({t["exit_host"] for t in tunnels})
            routes = {t["route_id"]: t["exit_host"] for t in tunnels}
            self._send_json(200, {"ok": True, "exits": exits, "routes": routes, "count": len(tunnels)})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        # Expected path: /via/<target>/bot<TOKEN>/<method>
        m = re.match(r"^/via/([^/]+)(/bot[0-9A-Za-z:_-]+/[A-Za-z0-9_]+)$", self.path)
        if not m:
            self._send_json(400, {"ok": False, "error": "invalid relay path"})
            return
        target = urllib.parse.unquote(m.group(1)).strip()
        tg_path = m.group(2)

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 256 * 1024:
            self._send_json(400, {"ok": False, "error": "invalid body size"})
            return
        body_text = self.rfile.read(length).decode("utf-8", "replace")

        tunnels = discover_tunnels()
        if not tunnels:
            self._send_json(503, {"ok": False, "error": "no active exit tunnels on this gateway"})
            return

        if target.lower() == "auto":
            # Try distinct exit hosts in order (prefer unique exits)
            seen_exits: set[str] = set()
            candidates = []
            for t in tunnels:
                if t["exit_host"] not in seen_exits:
                    seen_exits.add(t["exit_host"])
                    candidates.append(t)
        else:
            candidates = [
                t for t in tunnels
                if t["exit_host"].lower() == target.lower()
                or t["route_id"].lower() == target.lower()
                or t["slot"].lower() == target.lower()
            ]
            if not candidates:
                self._send_json(
                    404,
                    {"ok": False, "error": f"exit tunnel '{target}' not found on gateway"},
                )
                return

        last_err = "unknown error"
        for cand in candidates:
            try:
                status, resp_body = forward_via_pid(cand["pid"], tg_path, body_text)
                raw = resp_body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Anet-Exit-Used", cand["exit_host"])
                self.send_header("X-Anet-Tunnel-Slot", cand["slot"])
                self.end_headers()
                self.wfile.write(raw)
                return
            except Exception as exc:
                last_err = f"{cand['exit_host']}: {exc}"

        self._send_json(502, {"ok": False, "error": f"all exit tunnels failed ({last_err})"})


def main() -> int:
    bind = os.environ.get("ANET_TG_RELAY_BIND", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("ANET_TG_RELAY_PORT", "18499"))
    srv = ThreadingHTTPServer((bind, port), RelayHandler)
    print(f"anet-tg-relay listening on {bind}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
