"""Compare receiver-side throughput with a fixed grant, first without shaping, then with TC/XDP."""
import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from test_client_upgrade_live import prepare, start, run_pair
from test_netns_matrix import inside, run

HERE = Path(__file__).resolve()


def auth(config_path):
    config = json.loads(config_path.read_text())

    class Grant(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("X-Auth-Key") != config["token"]:
                self.send_error(403)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/check_access":
                result = dict(allowed=request.get("fingerprint") == config["fingerprint"], message="QA",
                              speed_limit_kbps=config["kbps"], user_id="00000000-0000-4000-8000-000000000001")
            else:
                result = {}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
    HTTPServer(("192.0.2.2", 3050), Grant).serve_forever()


def experiment(args, work):
    state = prepare(work)
    server_file = state / "server.toml"
    base = server_file.read_text()
    allow = re.search(r"^allowed_clients = (.*)$", base, re.M).group(1)
    token = re.search(r"^auth_server_token = (.*)$", base, re.M).group(1)
    grants = work / "grants"
    grants.write_text(json.dumps(dict(fingerprint=json.loads(allow)[0], token=json.loads(token), kbps=args.kbps)))
    base = base.replace("allowed_clients = " + allow, "allowed_clients = []").replace(
        "auth_servers = []", 'auth_servers = ["http://192.0.2.2:3050"]')
    server_file.chmod(0o600)
    measurements = {}
    with ExitStack() as stack:
        start(stack, "receiver", [sys.executable, str(HERE), "auth", str(grants)], work/"auth.log")
        start(stack, "receiver", [sys.executable, str(HERE.with_name("tunnel.py")), "echo"], work/"echo.log")
        speed = str(HERE.with_name("throughput.py"))
        start(stack, "receiver", [sys.executable, speed, "serve"], work/"throughput.log")
        for enabled in (False, True):
            server_file.write_text(base + "\n[shaper]\nenabled = " + str(enabled).lower() + "\n")
            def measure():
                if enabled:
                    link = json.loads(run("ip", "-n", "server", "-j", "-d", "link", "show", "anet-server"))[0]
                    assert link["xdp"]["prog"]["id"] > 0 and link["qdisc"] == "fq", link
                    log = (work/"server.log").read_text()
                    assert "Attached tc_egress + xdp_ingress" in log and f"-> {args.kbps} kbps" in log
                rows = run(*inside("client", sys.executable, speed, "measure", "192.0.2.2", "--interface",
                                   "anet-client", "--seconds", "4", "--runs", "1"))
                measurements[enabled] = {r["direction"]: r["Mbps"] for r in map(json.loads, rows.splitlines())}
                print("enabled", enabled, measurements[enabled], flush=True)
            run_pair(args, work, state, args.server, args.client, measure)
    for direction, shaped in measurements[True].items():
        baseline = measurements[False][direction]
        if not (0 < shaped <= 1.3 * args.kbps / 1000 and baseline >= 2 * shaped):
            raise RuntimeError(f"{direction}: baseline {baseline} Mbps, shaped {shaped} Mbps, limit {args.kbps} kbps")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--kbps", type=int, default=2000)
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.isolated:
        if os.geteuid() != 0:
            parser.error("run on a dedicated Linux test host with BPF and NET_ADMIN privileges")
        subprocess.run(["unshare", "-nm", sys.executable, str(HERE), *sys.argv[1:], "--isolated"], check=True,
                       env=dict(os.environ, SHAPER_PARENT_NETNS=os.readlink("/proc/self/ns/net")))
        return
    if os.readlink("/proc/self/ns/net") == os.environ["SHAPER_PARENT_NETNS"]:
        raise RuntimeError("expected private network namespace")
    with tempfile.TemporaryDirectory() as work:
        experiment(args, Path(work))


if __name__ == "__main__":
    if sys.argv[1:2] == ["auth"]:
        auth(Path(sys.argv[2]))
    else:
        main()
