"""Read an ANet release manifest and resolve a verified deployment or client."""
import argparse
import json
import re
import sqlite3
from pathlib import Path

SERVICES = {
    "standalone": ("SERVER_IMAGE", "NETNS_IMAGE"),
    "webui": ("SERVER_IMAGE", "NETNS_IMAGE", "AUTH_IMAGE", "WEBUI_IMAGE", "POSTGRES_IMAGE"),
}
SERVICES["anet-easy"] = SERVICES["webui"] + ("CLIENT_IMAGE", "INGRESS_IMAGE", "PROMETHEUS_IMAGE", "BLACKBOX_IMAGE", "GRAFANA_IMAGE")
SERVICES["panel-update"] = ("WEBUI_IMAGE",)
OPTIONAL_SERVICES = {"anet-easy": ("DNS_IMAGE",), "panel-update": ("AUTH_IMAGE",)}
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")


def selected_roles(images, profile):
    return SERVICES[profile] + tuple(role for role in OPTIONAL_SERVICES.get(profile, ()) if role in images)


def resolve_images(manifest, profile, platform):
    if platform and platform not in manifest["platforms"]:
        raise ValueError(f"platform {platform} is missing from the release")
    result = {}
    for service in selected_roles(manifest["images"], profile):
        ref = manifest["images"][service]
        if not isinstance(ref, str) or not IMAGE.fullmatch(ref):
            raise ValueError(f"{service} requires an immutable OCI reference")
        result[service] = ref
    return result


def verified_manifest(path, require_clients=True):
    data = json.loads(path.read_text())
    if data["format"] != 1 or not re.fullmatch("[0-9a-f]{40}", data["server_commit"]):
        raise ValueError("expected format 1 and full server commit")
    if not require_clients:
        return data
    if not isinstance(data["verified_clients"], list):
        raise ValueError("verified_clients must be a list")
    for entry in data["verified_clients"]:
        if not all(isinstance(entry[key], str) and entry[key] for key in ("client", "config_schema", "transport")):
            raise ValueError("invalid verified_clients entry")
    return data


def client_supported(manifest, client, schema, transport):
    return any((e["client"], e["config_schema"], e["transport"]) == (client, schema, transport)
               for e in manifest["verified_clients"])


def record_issuance(history, profile, client, schema, transport, server):
    with sqlite3.connect(history) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS issued (
            profile TEXT, client TEXT, schema TEXT, transport TEXT, server TEXT,
            PRIMARY KEY (profile, client, schema, transport, server))""")
        db.execute("INSERT OR IGNORE INTO issued VALUES (?, ?, ?, ?, ?)",
                   (profile, client, schema, transport, server))


def upgrade_report(manifest, history):
    with sqlite3.connect(history.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return [dict(profile=p, client=c, config_schema=s, transport=t, issued_server=server,
                     supported=client_supported(manifest, c, s, t))
                for p, c, s, t, server in db.execute(
                    "SELECT profile,client,schema,transport,server FROM issued ORDER BY 1,2,3,4,5")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    env = commands.add_parser("env")
    env.add_argument("manifest", type=Path)
    env.add_argument("profile", choices=SERVICES)
    env.add_argument("--platform", help="target OCI platform, e.g. linux/arm64")
    check = commands.add_parser("check-client")
    check.add_argument("manifest", type=Path)
    for key in ("client", "config_schema", "transport"):
        check.add_argument(key)
    upgrade = commands.add_parser("pre-upgrade")
    upgrade.add_argument("manifest", type=Path)
    upgrade.add_argument("history", type=Path)
    args = parser.parse_args()
    try:
        data = verified_manifest(args.manifest, require_clients=args.action != "env")
        if args.action == "pre-upgrade":
            rows = upgrade_report(data, args.history)
            print(json.dumps(dict(candidate=data["server_commit"], profiles=rows), indent=2))
            if not all(row["supported"] for row in rows):
                parser.exit(2)
            return
        if args.action == "env":
            for key, value in resolve_images(data, args.profile, args.platform).items():
                print(f"{key}={value}")
        else:
            requested = {k: getattr(args, k) for k in ("client", "config_schema", "transport")}
            if not client_supported(data, args.client, args.config_schema, args.transport):
                raise ValueError(f"no verified client combination for {requested}")
            print(data["server_commit"])
    except (KeyError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Invalid or incompatible release: {exc}\n")


if __name__ == "__main__":
    main()
