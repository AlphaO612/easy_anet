"""Prepare a local or HTTPS distribution from a committed installer and a verified image bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "contrib/deploy/scripts"))
from release import SERVICES


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(bundle, dest, url, ref):
    parsed = urlsplit(url or "")
    if url is not None and (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(char.isspace() for char in url)):
        raise ValueError("--base-url requires a public or private HTTPS directory without credentials")
    dest = dest.absolute()
    if dest.exists():
        raise ValueError("choose a new output directory for this release")
    commit = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                                     cwd=REPO, text=True).strip()
    template = subprocess.check_output(["git", "show", commit + ":contrib/deploy/distribution/bootstrap.sh"],
                                       cwd=REPO, text=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        stage = Path(tmp) / "release"
        stage.mkdir()
        manifest_file = stage / "manifest.json"
        shutil.copyfile(bundle / "manifest.json", manifest_file)
        manifest = json.loads(manifest_file.read_text())
        if (manifest.get("format"), manifest.get("platform"), manifest.get("archive")) != (1, "linux/amd64", "images.tar"):
            raise ValueError("expected a linux/amd64 image bundle")
        images = manifest["images"]
        if not set(SERVICES["anet-easy"]) <= images.keys() or not all(
                re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in images.values()):
            raise ValueError("bundle lacks immutable image IDs for the full deployment")
        archive = stage / "images.tar"
        shutil.copyfile(bundle / "images.tar", archive)
        images_sum = sha256(archive)
        if images_sum != manifest["sha256"]:
            raise ValueError("images.tar differs from the bundle manifest")
        source = stage / "source.tar.gz"
        subprocess.run(["git", "archive", "--format=tar.gz", "-o", source, commit], cwd=REPO, check=True)
        # Estimate filesystem allocation for the extracted Git archive, including directory entries.
        with tarfile.open(source, "r:gz") as contents:
            unpack_bytes = sum(4096 + ((member.size + 4095) // 4096) * 4096 for member in contents)
        download_bytes = sum(p.stat().st_size for p in (manifest_file, archive, source))
        substitutions = {"UNPACK_BYTES": str(unpack_bytes), "DOWNLOAD_BYTES": str(download_bytes),
                         "SOURCE_SHA256": sha256(source), "MANIFEST_SHA256": sha256(manifest_file),
                         "IMAGES_SHA256": images_sum, "BASE_URL": shlex.quote((url or "").rstrip("/"))}
        rendered = re.sub(r"@(SOURCE_SHA256|MANIFEST_SHA256|IMAGES_SHA256|BASE_URL|UNPACK_BYTES|DOWNLOAD_BYTES)@",
                          lambda match: substitutions[match[1]], template)
        launcher = stage / ("install.sh" if url is None else "bootstrap.sh")
        launcher.write_text(rendered)
        launcher.chmod(0o755)
        if url is None:
            for name in ("send-to-vps.ps1", "send-to-vps.sh", "START-HERE.md"):
                helper = stage / name
                with helper.open("wb") as out:
                    subprocess.run(["git", "show", f"{commit}:contrib/deploy/distribution/{name}"],
                                   cwd=REPO, stdout=out, check=True)
                if name.endswith(".sh"):
                    helper.chmod(0o755)
        stage.rename(dest)
    print(f"Installer commit {commit}")
    if url is None:
        print(f"Copy {dest} to the first VPS, then run ./install.sh in the copied directory")
        return
    print(f"Upload {dest} to the configured HTTPS directory, then run")
    print("curl --proto '=https' --proto-redir '=https' --netrc-optional -fsSL "
          + shlex.quote(url.rstrip("/") + "/bootstrap.sh") + " | bash")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("destination", type=Path)
    delivery = parser.add_mutually_exclusive_group(required=True)
    delivery.add_argument("--base-url", help="HTTPS directory for curl installation")
    delivery.add_argument("--local", action="store_true", help="prepare a self-contained directory for SSH transfer")
    parser.add_argument("--ref", default="HEAD", help="installer Git revision (default: HEAD)")
    args = parser.parse_args()
    try:
        prepare(args.bundle, args.destination, args.base_url, args.ref)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
