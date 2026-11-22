"""Initialize the bundled central collector for its enrolled local source node."""
import argparse
import json
import os
from pathlib import Path
import secrets
import time
import urllib.request
import tempfile
import re
from uuid import UUID


def prepare(state):
    root = Path(__file__).resolve().parents[2] / "monitoring"
    base = (root / "prometheus.yml").read_text().replace("global:\n", "global:\n  scrape_interval: 10s\n  scrape_timeout: 5s\n")
    scrape = (root / "agent.example.yml").read_text().split("scrape_configs:\n", 1)[1]
    server = (state / "server.toml").read_text()
    section = re.search(r"(?ms)^\[control_plane\][ \t]*$([^\[]*)", server)
    node = None
    if section:
        match = re.search(r'(?m)^node_id\s*=\s*"([0-9a-fA-F-]+)"\s*$', section[1])
        if not match:
            raise ValueError("missing enrolled node ID in control_plane")
        node = UUID(match[1])
    if node:
        # Attach the source to samples before recording rules run.
        scrape = scrape.replace("    relabel_configs:", """    relabel_configs:
      - target_label: source_node
        replacement: """ + str(node))
    else:
        scrape = "  []\n"
    with tempfile.NamedTemporaryFile("w", dir=state, delete=False) as out:
        out.write(base + "scrape_configs:\n" + scrape)
        os.fchmod(out.fileno(), 0o444)
    os.replace(out.name, state / "central-prometheus.yml")
    password = state / "grafana-password"
    if not password.exists():
        with tempfile.NamedTemporaryFile("w", dir=state, delete=False) as out:
            out.write(secrets.token_urlsafe(32) + "\n")
            os.fchmod(out.fileno(), 0o444)
        os.replace(out.name, password)


def wait_ready(prometheus, grafana):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for address, path in ((prometheus, "/-/ready"), (grafana, "/api/health")):
        if not re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{0,4}", address):
            raise SystemExit(f"Missing published loopback port for {path}")
        url = f"http://{address}{path}"
        # First boot runs hundreds of Grafana database migrations on VPS storage.
        deadline = time.monotonic() + 300
        while True:
            try:
                with opener.open(url, timeout=3) as response:
                    if path == "/api/health" and json.load(response).get("database") != "ok":
                        raise ValueError("Grafana database unavailable")
                break
            except (OSError, ValueError) as error:
                if time.monotonic() >= deadline:
                    raise SystemExit(f"{url} readiness failed: {error}") from error
                time.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("state", nargs="?", type=Path)
    group.add_argument("--check", nargs=2, metavar=("PROMETHEUS_ADDRESS", "GRAFANA_ADDRESS"))
    args = parser.parse_args()
    if args.check:
        wait_ready(*args.check)
    else:
        prepare(args.state)
