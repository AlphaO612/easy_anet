"""Package the verified, panel-only update for a tagged linux/amd64 release."""
import argparse
import hashlib
from pathlib import Path
import re
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
from bundle import pack


def prepare(directory, image, tag):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Invalid release tag")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "bundle"
        pack(bundle, ["WEBUI_IMAGE="+image])
        archive = directory / "panel-update.tar.gz"
        with tarfile.open(archive,"w:gz",compresslevel=2) as tar:
            tar.add(bundle,arcname="bundle")
            for name in ("scripts/update-panel.py", "scripts/bundle.py", "scripts/release.py", "scripts/topology.py",
                         "compose/compose.yaml", "compose/webui.yaml", "compose/probe-management.yaml"):
                tar.add(HERE.parent / name,arcname=name)
        url = f"https://github.com/AlphaO612/easy_anet/releases/download/{tag}/{archive.name}"
        launcher = (HERE / "panel-update.sh").read_text().replace("@PANEL_URL@",url)
        (directory / "panel-update.sh").write_text(launcher.replace("@PANEL_SHA@",hashlib.sha256(archive.read_bytes()).hexdigest()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("image")
    parser.add_argument("tag")
    args = parser.parse_args()
    prepare(args.directory, args.image, args.tag)
