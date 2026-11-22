"""Initialize the panel signing identity once in the private deployment state."""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def derive(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout

def initialize(state):
    target = state / "probe-signing"
    if target.exists():
        for name in ("seed", "public"):
            if len(base64.b64decode((target / name).read_text().strip(), validate=True)) != 32:
                raise ValueError(f"invalid existing {name}")
        return
    with tempfile.TemporaryDirectory(dir=state) as staging:
        pair = Path(staging) / "pair"
        pair.mkdir(mode=0o700)
        pem = derive("genpkey", "-algorithm", "Ed25519")
        for name, options in (("seed", []), ("public", ["-pubout"])):
            encoded = derive("pkey", *options, "-outform", "DER", data=pem)
            material = pair / name
            material.write_bytes(base64.b64encode(encoded[-32:]) + b"\n")
            material.chmod(0o444)
        os.rename(pair, target)

if __name__ == "__main__":
    initialize(Path(sys.argv[1]))
