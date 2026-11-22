"""Install a small release over an older image bundle in a fresh PTY."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "distribution"))
import package


class UpdateDelivery(unittest.TestCase):
    def test_pinned_update_and_existing_image_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            deploy = repo / "contrib/deploy"
            templates = deploy / "distribution"
            templates.mkdir(parents=True)
            for name in ("bootstrap.sh", "update.sh"):
                shutil.copy(package.REPO / "contrib/deploy/distribution" / name, templates)
            installer = deploy / "install.sh"
            installer.write_text('#!/bin/bash\nset -eu\ntest -t 0\n'
                                 '[[ "$ANET_BUNDLE_DIR/images.tar" -ef "$OLD_ARCHIVE" ]]\n'
                                 'echo "New wizard ready"\n')
            installer.chmod(0o755)
            for args in (["init"], ["add", "-f", "."], ["-c", "user.name=test", "-c", "user.email=test@local",
                                               "commit", "-m", "release fixture"]):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            old = root / "old bundle"
            old.mkdir()
            (old / "install.sh").write_text("previous installer")
            archive = old / "images.tar"
            archive.write_bytes(os.urandom(128 * 1024))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = old / "manifest.json"
            manifest.write_text(json.dumps(dict(format=1, platform="linux/amd64", archive="images.tar", sha256=digest,
                                images={k: "sha256:" + digest for k in package.SERVICES["anet-easy"]})))
            update = root / "assets"
            with patch.object(package, "REPO", repo):
                package.prepare(old, update, None, "HEAD", "v1.2.3")
            launcher = update / "anet-easy-update.sh"
            small_tar = update / "anet-easy-update.tar.gz"
            with tarfile.open(small_tar) as files:
                self.assertEqual(files.getnames(), ["install.sh", "source.tar.gz"])
            self.assertLess(small_tar.stat().st_size, archive.stat().st_size)
            full_tar = small_tar.read_bytes()
            old_manifest = manifest.read_bytes()

            stub = root / "bin"
            stub.mkdir()
            curl = stub / "curl"
            # The production updater pins its asset URL. Route just this request to the fixture.
            curl.write_text('#!/bin/sh\nset -eu\nwhile [ "$1" != --output ]; do shift; done\n'
                            'test "$4" = "https://github.com/AlphaO612/easy_anet/releases/download/v1.2.3/anet-easy-update.tar.gz"\n'
                            'cp "$ASSET" "$2"\necho downloaded >> "$REQUESTS"\n')
            curl.chmod(0o755)
            requests = root / "requests"
            env = dict(os.environ, PATH=os.pathsep.join([str(stub), os.environ["PATH"]]), ASSET=str(small_tar),
                       XDG_DATA_HOME=str(root / "cache"), OLD_ARCHIVE=str(archive), REQUESTS=str(requests))
            def launch(command=None):
                if command is None:
                    command = "cat " + shlex.quote(str(launcher)) + " | bash"
                return subprocess.run(["script", "-qec", command, "/dev/null"], cwd=old, env=env,
                                      capture_output=True, text=True, timeout=30)
            manifest.write_bytes(b"unmatched bundle")
            self.assertNotEqual(launch().returncode, 0)
            self.assertFalse(requests.exists())
            manifest.write_bytes(old_manifest)
            small_tar.write_bytes(b"truncated")
            result = launch()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256", result.stdout)
            self.assertFalse(list(old.glob("anet-update-*")))
            self.assertFalse(list(old.glob(".anet-update.*")))
            small_tar.write_bytes(full_tar)

            result = launch()
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("New wizard ready", result.stdout)
            count = requests.read_text()
            self.assertEqual(launch().returncode, 0)
            self.assertEqual(count, requests.read_text())
            (installed,) = old.glob("anet-update-*")
            self.assertTrue((installed / "images.tar").is_symlink())
            self.assertTrue(archive.samefile(installed / "images.tar"))
            self.assertTrue(manifest.samefile(installed / "manifest.json"))
            self.assertEqual((old / "install.sh").read_text(), "previous installer")

            shutil.rmtree(installed)
            offline = launch(shlex.join(["bash", str(launcher), str(small_tar)]))
            self.assertEqual(offline.returncode, 0, offline.stdout)
            self.assertEqual(count, requests.read_text())
            archive.write_bytes(b"damaged")
            result = launch()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("New wizard ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
