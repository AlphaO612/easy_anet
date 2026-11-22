"""Compare an Ansible topology inventory with the panel and optionally publish it."""
import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from topology import api_request, canonical, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["diff", "apply"])
    parser.add_argument("api")
    parser.add_argument("token_file", type=Path)
    parser.add_argument("base_revision", type=int)
    args = parser.parse_args()
    desired = json.load(sys.stdin)
    token = args.token_file.read_text().strip()
    require(token, "empty admin token")
    path = "/profile-topology"
    active = api_request(args.api, path, token)
    revision = active["revision"]
    before = canonical(json.loads(active["manifest"]))
    after = canonical(desired)
    if args.operation == "diff":
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                       fromfile=f"panel@{revision}", tofile=f"inventory@{args.base_revision}"))
        print(json.dumps(dict(revision=revision, changed=False, diff=diff)))
        return
    require(revision == args.base_revision,
            f"revision conflict: inventory expects {args.base_revision}, panel has {revision}")
    changed = before != after
    if changed:
        active = api_request(args.api, path, token,
                          publication=dict(revision=args.base_revision, manifest=after))
        revision = active["revision"]
    print(json.dumps(dict(revision=revision, changed=changed, diff="")))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, TypeError, ValueError) as error:
        sys.exit(f"Topology reconciliation failed: {error}")
