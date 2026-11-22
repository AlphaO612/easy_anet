"""Exercise the actual curl | bash entry point on loopback HTTPS with a disposable installer."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "distribution"))
import package


class QuietHTTP(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/images.tar" and self.server.truncate_images:
            self.send_response(200)
            self.send_header("Content-Length", "256")
            self.end_headers()
            self.wfile.write(b"incomplete")
            return
        super().do_GET()

    def log_message(self, *args):
        pass


class Delivery(unittest.TestCase):
    def test_https_and_local_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy = root / "repo/contrib/deploy"
            distribution = deploy / "distribution"
            distribution.mkdir(parents=True)
            shutil.copy(package.REPO / "contrib/deploy/distribution/bootstrap.sh", distribution)
            helper_files = ("send-to-vps.ps1", "send-to-vps.sh", "START-HERE.md")
            for name in helper_files:
                (distribution / name).write_text("fixture " + name + "\n")
            installer = deploy / "install.sh"
            installer.write_text("#!/bin/sh\nset -eu\ntest -t 0\ntest \"$#\" = 0\n"
                                 'cmp "$ANET_BUNDLE_DIR/images.tar" "$EXPECTED_IMAGES"\n'
                                 'echo "Verified installer started"\n')
            installer.chmod(0o755)
            repo = root / "repo"
            def git(*args):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            git("init")
            git("add", "-f", ".")
            git("-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-m", "fixture")
            bundle = root / "bundle"
            bundle.mkdir()
            archive = bundle / "images.tar"
            archive.write_bytes(b"isolated image transfer fixture")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            (bundle / "manifest.json").write_text(json.dumps(dict(format=1, platform="linux/amd64",
                archive="images.tar", sha256=digest, images={k: "sha256:" + digest for k in package.SERVICES["anet-easy"]})))

            local_dist = root / "local release"
            with patch.object(package, "REPO", repo):
                package.prepare(bundle, local_dist, None, "HEAD")
            self.assertTrue(os.access(local_dist / "install.sh", os.X_OK))
            for name in helper_files:
                self.assertEqual((local_dist / name).read_bytes(), (distribution / name).read_bytes())
            self.assertTrue(os.access(local_dist / "send-to-vps.sh", os.X_OK))
            def local_launch(cache="local-cache", **extra_env):
                env = dict(os.environ, EXPECTED_IMAGES=str(archive), XDG_DATA_HOME=str(root / cache), **extra_env)
                return subprocess.run(["script", "-qec", shlex.quote(str(local_dist / "install.sh")), "/dev/null"],
                                      env=env, capture_output=True, timeout=30, text=True)
            result = local_launch()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Verified installer started", result.stdout)
            self.assertFalse(list((root / "local-cache").rglob("images.tar")))
            for expected in ("1/3", "2/3", "3/3", "100%", "SHA-256 images.tar", "Распаковка source.tar.gz"):
                self.assertIn(expected, result.stdout)
            no_tty = subprocess.run([local_dist / "install.sh"], start_new_session=True, capture_output=True, text=True)
            self.assertNotEqual(no_tty.returncode, 0)
            self.assertIn("ssh", no_tty.stderr)
            # A fresh cache on a full filesystem should fail before hashing or extracting.
            stub = root / "stub"
            stub.mkdir()
            df = stub / "df"
            df.write_text("#!/bin/sh\nprintf 'disk 100 100 0 100%% /mount\\n'\n")
            df.chmod(0o755)
            no_space = local_launch("full-disk", PATH=os.pathsep.join([str(stub), os.environ["PATH"]]))
            self.assertNotEqual(no_space.returncode, 0)
            self.assertIn("Недостаточно места", no_space.stdout)
            self.assertNotIn("Verified installer started", no_space.stdout)
            hidden = root / "missing-manifest"
            (local_dist / "manifest.json").rename(hidden)
            missing = local_launch()
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("Передайте этот файл", missing.stdout)
            hidden.rename(local_dist / "manifest.json")
            for filename in ("manifest.json", "source.tar.gz", "images.tar"):
                file = local_dist / filename
                valid_bytes = file.read_bytes()
                file.write_bytes(b"broken")
                damaged = local_launch()
                self.assertNotEqual(damaged.returncode, 0)
                self.assertIn("SHA-256", damaged.stdout)
                self.assertNotIn("Verified installer started", damaged.stdout)
                file.write_bytes(valid_bytes)

            dest = root / "dist"
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHTTP, directory=str(dest)))
            server.truncate_images = False
            self.addCleanup(server.server_close)
            cert, key = root / "cert.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                            "-keyout", key, "-out", cert], check=True, capture_output=True)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            url = f"https://127.0.0.1:{server.server_port}"
            with patch.object(package, "REPO", repo):
                package.prepare(bundle, dest, url, "HEAD")

            def launch(cache):
                env = dict(os.environ, CURL_CA_BUNDLE=str(cert), XDG_DATA_HOME=str(root / cache),
                           EXPECTED_IMAGES=str(archive), NO_PROXY="127.0.0.1")
                command = f"curl --proto '=https' -fsSL {shlex.quote(url)}/bootstrap.sh | bash"
                return subprocess.run(["script", "-qec", command, "/dev/null"], env=env,
                                      capture_output=True, timeout=30, text=True)
            self.assertFalse(any((dest / name).exists() for name in helper_files))
            server.truncate_images = True
            interrupted = launch("interrupted")
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertIn("Загрузка images.tar прервалась", interrupted.stdout)
            self.assertNotIn("Verified installer started", interrupted.stdout)
            self.assertFalse(list((root / "interrupted/anet/releases").glob(".download.*")))
            server.truncate_images = False
            self.assertEqual(launch("interrupted").returncode, 0)
            valid = launch("cache")
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            self.assertIn("Verified installer started", valid.stdout)
            (dest / "images.tar").write_bytes(b"broken")
            self.assertEqual(launch("cache").returncode, 0)
            damaged = launch("new-cache")
            self.assertNotEqual(damaged.returncode, 0)
            self.assertIn("SHA-256", damaged.stdout)
            self.assertNotIn("Verified installer started", damaged.stdout)
            self.assertFalse(list((root / "new-cache/anet/releases").glob(".download.*")))


if __name__ == "__main__":
    unittest.main()
