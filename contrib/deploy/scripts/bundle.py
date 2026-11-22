"""Pack and load content-addressed Docker images for offline Compose deployment."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import sys

ID = re.compile(r"sha256:[0-9a-f]{64}")
from release import SERVICES, OPTIONAL_SERVICES, selected_roles

ROLES = set(SERVICES["anet-easy"] + OPTIONAL_SERVICES["anet-easy"])


def checksum(file):
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(4 * 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def identity(ref):
    output = subprocess.check_output(["docker", "image", "inspect", "--format",
                                      "{{.Id}} {{.Os}}/{{.Architecture}}", "--", ref], text=True)
    image, platform = output.strip().split()
    if not ID.fullmatch(image) or platform != "linux/amd64":
        raise ValueError(f"expected linux/amd64 image: {ref}")
    return image


def pack(dest, specs):
    images = {}
    for spec in specs:
        role, ref = spec.split("=", 1)
        if role not in ROLES or role in images:
            raise ValueError(f"unexpected or duplicate role {role}")
        images[role] = identity(ref)
    dest.mkdir(mode=0o700)
    archive = dest / "images.tar"
    subprocess.run(["docker", "image", "save", "-o", archive, *sorted(set(images.values()))], check=True)
    with tarfile.open(archive, mode="r:") as saved:
        entries = archive_entries(saved)
        images = {role: saved_config(saved, entries, engine_id) for role, engine_id in images.items()}
    with archive.open("rb") as file:
        digest = checksum(file)
    (dest / "manifest.json").write_text(json.dumps(dict(format=1, platform="linux/amd64",
            archive=archive.name, sha256=digest, images=images), indent=2) + "\n")


def archive_entries(archive):
    def metadata(name):
        member = archive.getmember(name)
        if not member.isfile() or member.size > 16 * 1024 * 1024:
            raise ValueError(f"unexpected Docker archive metadata: {name}")
        return archive.extractfile(member).read()

    entries = json.loads(metadata("manifest.json"))
    return {"sha256:" + hashlib.sha256(metadata(entry["Config"])).hexdigest(): entry
            for entry in entries}


def saved_config(archive, entries, image_id):
    if image_id in entries:
        return image_id
    # containerd-backed Docker addresses images by manifest or index digest.
    # Verify the descriptor chain back to the config referenced by Docker's archive manifest.
    for _ in range(3):
        if not ID.fullmatch(image_id):
            break
        member = archive.getmember("blobs/sha256/" + image_id.split(":")[1])
        if not member.isfile() or member.size > 16 * 1024 * 1024:
            break
        raw = archive.extractfile(member).read()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != image_id:
            break
        descriptor = json.loads(raw)
        if "config" in descriptor:
            config = descriptor["config"]["digest"]
            if config in entries:
                return config
            break
        children = [m["digest"] for m in descriptor.get("manifests", [])
                    if m.get("platform", {}).get("os") == "linux"
                    and m["platform"].get("architecture") == "amd64"]
        if len(children) != 1:
            break
        image_id = children[0]
    raise ValueError("saved image descriptor does not resolve to exactly one linux/amd64 config")


def runtime_tag(config):
    return "anet-bundle-config:" + config.removeprefix("sha256:")


def import_images(archive, entries, selected):
    # Supply only verified configs, their layers and the freshly constructed tag mapping.
    # Legacy untagged archives otherwise become unaddressable by config ID on Docker 29.
    manifest = [dict(entries[config], RepoTags=[runtime_tag(config)]) for config in sorted(selected)]
    names = {name for entry in manifest for name in [entry["Config"], *entry["Layers"]]}
    command = ["docker", "image", "load"]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=sys.stderr) as process:
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|", bufsize=1024 * 1024) as output:
                output.copybufsize = 1024 * 1024
                for name in sorted(names):
                    member = archive.getmember(name)
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise ValueError(f"invalid image member: {name}")
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"missing image member data: {name}")
                    # Docker save may represent a shared layer with an archive-local link.
                    header = tarfile.TarInfo(name)
                    header.size = source.seek(0, 2)
                    source.seek(0)
                    output.addfile(header, source)
                data = json.dumps(manifest).encode()
                header = tarfile.TarInfo("manifest.json")
                header.size = len(data)
                output.addfile(header, io.BytesIO(data))
        finally:
            process.stdin.close()
        if process.wait():
            raise ValueError("Docker rejected the verified image archive")


def load_bundle(dest, profile):
    manifest = json.loads((dest / "manifest.json").read_text())
    if (manifest["format"], manifest["platform"], manifest["archive"]) != (1, "linux/amd64", "images.tar"):
        raise ValueError("unsupported bundle format, platform or archive")
    images = manifest["images"]
    roles = selected_roles(images, profile)
    required = set(roles)
    if not required <= images.keys() <= ROLES or not all(ID.fullmatch(v) for v in images.values()):
        raise ValueError("missing role or invalid image identity")
    with (dest / manifest["archive"]).open("rb") as file:
        if checksum(file) != manifest["sha256"]:
            raise ValueError("archive SHA-256 mismatch")
        file.seek(0)
        with tarfile.open(fileobj=file, mode="r:") as archive:
            entries = archive_entries(archive)
            if not set(images.values()) <= entries.keys():
                raise ValueError("bundle references an image config missing from the archive")
            selected = {images[role] for role in required}
            import_images(archive, entries, selected)
    resolved = {config: identity(runtime_tag(config)) for config in selected}
    return {role: resolved[images[role]] for role in roles}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("pack")
    build.add_argument("directory", type=Path)
    build.add_argument("images", nargs="+", metavar="ROLE=LOCAL_IMAGE")
    load = commands.add_parser("load")
    load.add_argument("directory", type=Path)
    load.add_argument("profile", choices=["standalone","webui","anet-easy"])
    args = parser.parse_args()
    try:
        if args.action == "pack":
            pack(args.directory, args.images)
        else:
            for role, image in load_bundle(args.directory, args.profile).items():
                print(f"{role}={image}")
    except (KeyError, OSError, ValueError, TypeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        parser.exit(1, f"Image bundle error: {exc}\n")


if __name__ == "__main__":
    main()
