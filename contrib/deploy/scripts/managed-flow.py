"""Reconcile one isolated native entry profile from the registered node's signed topology."""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

from flow_node import NodeAPI
from flow_adapter import attach
from topology import atomic_write, read, validate

HERE = Path(__file__).resolve().parent
bootstrap = runpy.run_path(str(HERE / "init-state.py"))
make_overlay = runpy.run_path(str(HERE / "flow-compose.py"))["overlay"]


def command(*args):
    return subprocess.check_output(args, text=True, timeout=180).strip()


def write_changed(path, value):
    if not path.exists() or path.read_text() != value:
        atomic_write(path, value)


class Entry:
    def __init__(self, settings, state):
        self.local = settings
        self.state = state
        self.profile = settings["profile"]
        self.project = "anet-flow-" + hashlib.sha256(os.fsencode(state)).hexdigest()[:16]
        self.compose = ["docker", "compose", "-p", self.project, "-f", str(HERE.parent / "compose/compose.yaml"),
                        "-f", str(state / "flow.json")]
        self.worker = None
        self.applied = None
        self.last_check = 0

    def stop_worker(self):
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            self.worker.wait(timeout=20)
        self.worker = None

    def down(self):
        self.stop_worker()
        if (self.state / "flow.json").exists():
            command(*self.compose, "down", "--remove-orphans")
        self.applied = None

    def uplink(self, name, path, revision, dev):
        # No remote identifier is interpreted as a filename or shell fragment.
        ident = hashlib.sha256(json.dumps([self.profile, name, path["exit"]]).encode()).hexdigest()
        key = self.state / (ident + ".key")
        if not key.exists():
            private, public = bootstrap["signing_identity"]()
            atomic_write(key, json.dumps([private, base64.b64encode(public).decode()]))
        private, public = read(key)
        peer = self.api.request("/uplink", dict(profile=self.profile, path=name, revision=revision, public_key=public))
        host = bootstrap["host_for_url"](peer["address"])
        if len(base64.b64decode(peer["public_key"], validate=True)) != 32 or not 0 < peer["port"] <= 65535:
            raise ValueError("invalid exit endpoint")
        text = (f'[main]\nmanual_routing = true\ntun_name = "{dev}"\n[keys]\n'
                f'private_key = {json.dumps(private)}\nserver_pub_key = {json.dumps(peer["public_key"])}\n'
                f'[[servers]]\nname = "uplink"\ndsn = "quic://{host}:{peer["port"]}"\n')
        # Content-addressing changes the bind mount on endpoint/key changes.
        config = self.state / (hashlib.sha256(text.encode()).hexdigest() + ".toml")
        write_changed(config, text)
        return str(config)

    def reconcile(self):
        self.api = NodeAPI(self.local["api"], Path(self.local["node_config"]), Path(self.local["signing_public"]))
        published = self.api.request("/profile-topology")
        rev = published["revision"]
        cursor = self.state / "revision"
        if cursor.exists() and rev < int(cursor.read_text()):
            raise ValueError("refusing stale topology revision")
        manifest = json.loads(published["manifest"])
        validate(manifest)
        profile = manifest["profiles"].get(self.profile)
        if (not profile or profile.get("user", "") != self.local["user"]
                or sorted(profile.get("groups", [])) != sorted(self.local.get("groups", []))
                or profile.get("credential_id") != self.local.get("credential_id")
                or profile.get("ingress", "native") != self.local.get("ingress", "native")):
            if self.applied is not False:
                self.down()
                self.applied = False
            write_changed(cursor, str(rev))
            return
        if self.applied == rev and self.worker and self.worker.poll() is None:
            if time.monotonic() - self.last_check < 60:
                return
            self.last_check = time.monotonic()
            services = set(command(*self.compose, "config", "--services").splitlines())
            running = set(command(*self.compose, "ps", "--services", "--status", "running").splitlines())
            if services == running and command(*self.compose, "ps", "-q", "network") == self.holder:
                return
            self.stop_worker()
        binding = dict(profile=self.profile, user=self.local["user"], groups=self.local.get("groups", []),
                       source=os.environ["TUN_SUBNET"], paths={}, revision=rev,
                       credential_id=profile.get("credential_id"), ingress=profile.get("ingress", "native"))
        if len(profile["paths"]) > 256:
            raise ValueError("too many uplinks")
        direct = []
        for i, name in enumerate(sorted(profile["paths"])):
            path = manifest["paths"][name]
            dev = f"anet-up{i+1}"
            target = {"interface": dev, "table": 1000+i}
            if path["entry"] == path["exit"]:
                direct.append(name)
                if len(direct) > 1:
                    raise ValueError("only one direct path per entry")
                target.update(interface="eth0", gateway="127.0.0.1")
            elif path["transport"] == "quic":
                target["config"] = self.uplink(name, path, rev, dev)
            else:
                raise ValueError("managed uplinks require QUIC")
            binding["paths"][name] = target
        domain_rules = profile.get("require_domain") or any("domain" in rule for rule in profile.get("rules", []))
        if domain_rules or "dns_path" in profile:
            observations = self.state / "observations"
            observations.mkdir(mode=0o700, exist_ok=True)
            binding.update(dns_path=profile.get("dns_path", profile["paths"][0]),
                           dns_listen=str(ipaddress.IPv4Network(binding["source"])[2]),
                           dns_upstream=str(ipaddress.IPv4Address(self.local["dns"])),
                           dns_answers_file=str(observations / "answers.json"))
        rendered = make_overlay(binding)
        client_config = attach(rendered, self.local, self.state, binding.get("dns_listen"))
        overlay = json.dumps(rendered, sort_keys=True)
        # Drain existing conntrack marks when the identity of any path changes.
        identity = json.dumps([overlay, [[n, [manifest["paths"][n][k] for k in
                              ("entry", "exit", "external_ip", "transport")]] for n in sorted(binding["paths"])]])
        identity_file = self.state / "path-identity"
        if not identity_file.exists() or identity_file.read_text() != identity:
            self.down()
        write_changed(self.state / "flow.json", overlay)
        command(*self.compose, "up", "-d", "--pull", "never", "--no-build", "--wait")
        holder = command(*self.compose, "ps", "-q", "network")
        if direct:
            route, = json.loads(command("docker", "exec", holder, "ip", "-j", "-4", "route", "show", "default"))
            binding["paths"][direct[0]].update(interface=route["dev"], gateway=str(ipaddress.IPv4Address(route["gateway"])))
        write_changed(identity_file, identity)
        write_changed(self.state / "binding.json", json.dumps(binding))
        write_changed(cursor, str(rev))
        if self.worker is None or self.worker.poll() is not None:
            self.worker = subprocess.Popen([sys.executable, str(HERE / "flow-router.py"), holder,
                str(self.state / "binding.json"), "--api", self.local["api"], "--node-config",
                self.local["node_config"], "--signing-public", self.local["signing_public"]])
        self.api.request("/profile-config", dict(profile=self.profile, revision=rev,
                                                config=client_config))
        self.applied = rev
        self.holder = holder
        self.last_check = time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path)
    parser.add_argument("--withdraw", action="store_true")
    args = parser.parse_args()
    local = read(args.settings)
    os.umask(0o077)
    if "name=rootless" not in command("docker", "info", "--format", "{{json .SecurityOptions}}"):
        raise ValueError("select the rootless daemon")
    state = Path(local["state"]).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_mode & 0o077:
        raise ValueError("managed profile state must be private")
    with (state / "agent.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        net = ipaddress.IPv4Network(local["source"])
        os.environ.update(STATE_DIR=str(state / "entry"), TUN_SUBNET=str(net), TUN_ADDRESS=f"{net[2]}/{net.prefixlen}",
                          VPN_PORT=str(local["port"]), VPN_BIND=str(ipaddress.ip_address(local["bind"])))
        if local.get("ingress", "native") != "native":
            os.environ.update(VPN_PORT="0", VPN_BIND="127.0.0.1")
        bootstrap["initialize"](state / "entry", local["host"], local["port"], "standalone", str(net))
        agent = Entry(local, state)
        if args.withdraw:
            agent.down()
            return
        try:
            while True:
                try:
                    agent.reconcile()
                except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as err:
                    print(f"profile reconciliation failed: {err}", file=sys.stderr, flush=True)
                time.sleep(5)
        finally:
            agent.stop_worker()


if __name__ == "__main__":
    main()
