"""Attach a WireGuard or TLS/VLESS listener to a managed native entry."""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from topology import atomic_write, load_toml

HERE = Path(__file__).resolve().parent


def attach(overlay, local, state, listener):
    protocol = local.get("ingress", "native")
    if protocol == "native":
        config = (state / "entry/client.toml").read_text()
        if listener:
            config += f'\n[main]\ntun_name="anet-client"\ndns_server_list=["{listener}"]\n'
        return config
    if protocol not in ("wireguard", "vless"):
        raise ValueError("unknown ingress adapter")
    dns = str(ipaddress.IPv4Address(local["dns"]))
    directory = state / "adapter"
    if not directory.exists():
        with tempfile.TemporaryDirectory(dir=state) as staging:
            generated = Path(staging) / "profile"
            subprocess.run([os.sys.executable, str(HERE.parent.parent / "chain/issue-ingress.py"), protocol,
                            str(generated), "--host", local["host"], "--port", str(local["port"]),
                            "--dns", dns], check=True)
            if protocol == "vless":
                for field, dest in (("tls_cert", "tls.pem"), ("tls_key", "tls.key")):
                    shutil.copyfile(local[field], generated / "server" / dest)
                    (generated / "server" / dest).chmod(0o600)
            generated.rename(directory)
    keys = load_toml(state / "entry/client.toml")["keys"]
    uplink = directory / "entry.toml"
    if not uplink.exists():
        atomic_write(uplink, '[main]\nmanual_routing = true\ntun_name = "anet-uplink"\n[keys]\n'
                     + '\n'.join(f'{k} = {json.dumps(v)}' for k, v in keys.items())
                     + '\n[[servers]]\nname = "entry"\ndsn = "quic://network:4519"\n')
    # Reuse the same fail-closed namespace, UID policy and helpers as the
    # standalone ingress. Only its private ANet uplink can reach the entry.
    env = dict(os.environ, INGRESS_PATH="chain", INGRESS_PROTOCOL=protocol, INGRESS_BIND=local["bind"],
               INGRESS_PORT=str(local["port"]), INGRESS_TARGET="51820/udp" if protocol == "wireguard" else "8443/tcp",
               INGRESS_DIR=str(directory / "server"), UPLINK_CONFIG=str(uplink),
               TUN_SUBNET="10.175.0.0/29", TUN_ADDRESS="10.175.0.2/29")
    raw = subprocess.check_output(["docker", "compose", "-f", str(HERE.parent / "compose/compose.yaml"), "-f", str(HERE.parent / "compose/chain.yaml"),
                                   "-f", str(HERE.parent.parent / "chain/ingress.yaml"), "config", "--format", "json"],
                                  env=env, text=True, timeout=30)
    services = json.loads(raw)["services"]
    for name in ("network", "uplink", "uplink-route", "ingress"):
        svc = services[name]
        if name == "network":
            svc["ports"] = [p for p in svc["ports"] if int(p["target"]) != 4519]
        else:
            svc["network_mode"] = "service:adapter-network"
            svc["depends_on"] = {"adapter-network": {"condition": "service_healthy"}}
        overlay["services"]["adapter-" + name] = svc
    if protocol == "wireguard":
        config = (directory / "client.conf").read_text()
        return config.replace(f"DNS = {dns}\n", f"DNS = {listener or dns}\n")
    server_config = directory / "server/server.json"
    spec = json.loads(server_config.read_text())
    if spec["dns"]["servers"] != [listener or dns]:
        spec["dns"]["servers"] = [listener or dns]
        atomic_write(server_config, json.dumps(spec))
    overlay["services"]["adapter-ingress"]["labels"] = {"anet.dns": listener or dns}
    return (directory / "client.uri").read_text()
