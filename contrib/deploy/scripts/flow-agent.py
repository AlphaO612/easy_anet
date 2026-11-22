"""Run dedicated entry projects for the profiles assigned to this enrolled node."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import errno
import subprocess
import sys
import time
import uuid

from flow_node import NodeAPI
from topology import atomic_write, read

WORKER = Path(__file__).with_name("managed-flow.py")


def stop(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)


def port_available(bind, port, ingress):
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    kind = socket.SOCK_STREAM if ingress == "vless" else socket.SOCK_DGRAM
    with socket.socket(family, kind) as sock:
        try:
            sock.bind((bind, port))
        except OSError as err:
            if err.errno == errno.EADDRINUSE:
                return False
            raise
    return True


def supervise(settings):
    for key in ("NETNS_IMAGE", "SERVER_IMAGE", "CLIENT_IMAGE", "DNS_IMAGE", "INGRESS_IMAGE"):
        if key in settings["images"]:
            os.environ[key] = settings["images"][key]
    root = Path(settings["state"]).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("agent state must be private")
    first, last = settings["ports"]
    if not 1024 <= first <= last <= 65535:
        raise ValueError("choose an unprivileged UDP port range")
    state_file = root / "profiles.json"
    workers = {}
    lock = (root / "supervisor.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    saved = read(state_file) if state_file.exists() else {"slots": {}, "revision": 0}
    slots = saved["slots"]
    try:
        while True:
            try:
                api = NodeAPI(settings["api"], Path(settings["node_config"]), Path(settings["signing_public"]))
                publication = api.request("/profile-topology")
                if publication["revision"] < saved["revision"]:
                    raise ValueError("refusing stale assignment")
                wanted = json.loads(publication["manifest"])["profiles"]
                def owner(p):
                    return [p.get("user", ""), sorted(p.get("groups", [])), p.get("credential_id"), p.get("ingress", "native")]
                for name, slot in list(slots.items()):
                    if name in wanted and slot["owner"] == owner(wanted[name]):
                        continue
                    stop(workers.pop(name, None))
                    subprocess.run([sys.executable, str(WORKER), slot["settings"], "--withdraw"], check=True, timeout=240)
                    del slots[name]
                    atomic_write(state_file, json.dumps(saved))
                for name, p in wanted.items():
                    if name not in slots:
                        occupied = {s["port"] for s in slots.values()}
                        port = next((i for i in range(first, last+1) if i not in occupied and
                                     port_available(settings["bind"], i, p.get("ingress"))), None)
                        if port is None:
                            print("no free profile port in configured range", file=sys.stderr, flush=True)
                            continue
                        folder = root / str(uuid.uuid4())
                        folder.mkdir(mode=0o700)
                        allocated = dict(settings, profile=name, user=p.get("user", ""), groups=p.get("groups", []),
                                         credential_id=p.get("credential_id"), ingress=p.get("ingress", "native"), port=port, state=str(folder))
                        config = folder / "settings.json"
                        atomic_write(config, json.dumps(allocated))
                        slots[name] = dict(owner=owner(p), port=port, settings=str(config))
                        atomic_write(state_file, json.dumps(saved))
                    if name not in workers or workers[name].poll() is not None:
                        workers[name] = subprocess.Popen([sys.executable, str(WORKER), slots[name]["settings"]],
                                                         start_new_session=True)
                if saved["revision"] != publication["revision"]:
                    saved["revision"] = publication["revision"]
                    atomic_write(state_file, json.dumps(saved))
            except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as err:
                print(f"assignment update failed: {err}", file=sys.stderr, flush=True)
            time.sleep(5)
    finally:
        for worker in workers.values():
            stop(worker)
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path)
    os.umask(0o077)
    supervise(read(parser.parse_args().settings))
