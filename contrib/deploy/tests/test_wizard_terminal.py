import errno
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY / "scripts"))
import wizard
from wizard_ui import WizardError


def terminal_run(args, stdin=b"", extra_env=None):
    master, slave = pty.openpty()
    result = bytearray()
    try:
        env = {**os.environ, "TERM": "xterm-256color", "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("NO_COLOR", None)
        env.pop("ANET_PLAIN", None)
        env.update(extra_env or {})
        with subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env) as process:
            os.close(slave)
            slave = None
            if stdin:
                os.write(master, stdin)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                try:
                    data = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                result.extend(data)
            else:
                process.kill()
                raise TimeoutError("PTY scenario did not finish")
            return process.wait(timeout=2), bytes(result)
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)


class OperatorTerminal(unittest.TestCase):
    def test_shell_menu_and_plain_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flags, colored in (([], True), (["--plain"], False)):
                code, out = terminal_run([DEPLOY / "install.sh", *flags], b"x\n0\n", {"HOME": tmp})
                self.assertEqual(code, 0)
                self.assertIn("Главное меню".encode(), out)
                self.assertIn("Введите номер".encode(), out)
                self.assertEqual(b"\033[" in out, colored)

    def test_guide_returns_to_menu_and_header_fits_terminal(self):
        for width in (22, 72):
            with tempfile.TemporaryDirectory() as tmp:
                code, raw = terminal_run([DEPLOY / "install.sh"], b"2\n\n0\n",
                                         {"HOME": tmp, "COLUMNS": str(width)})
                self.assertEqual(code, 0)
                output = raw.decode()
                self.assertEqual(output.count("Главное меню"), 2)
                self.assertIn("6. Панель и клиентские профили", output)
                self.assertIn("› 1   Установить", output)
                self.assertIn("╭" + "─" * (min(66, width) - 2) + "╮", output)
                self.assertNotIn("Публичный IPv4", output)
                self.assertFalse((Path(tmp) / ".local/state/anet-wizard").exists())

    def test_animation_yields_terminal_to_authentication(self):
        script = '''
from wizard_ui import Terminal
import time
ui = Terminal()
with ui.activity("Доставка"):
    time.sleep(.55)
with ui.activity("Подключение", interactive=True):
    print("AUTH_BEGIN", flush=True)
    time.sleep(.25)
    print("AUTH_END", flush=True)
'''
        code, out = terminal_run([sys.executable, "-c", script], extra_env={"PYTHONPATH": str(DEPLOY / "scripts")})
        self.assertEqual(code, 0)
        self.assertGreater(out.count(b"\033[2K"), 2)
        self.assertNotIn(b"\033", out.split(b"AUTH_BEGIN")[1].split(b"AUTH_END")[0])
        self.assertEqual(out.count("Готово".encode()), 2)

    def test_failed_worker_never_displays_raw_diagnostics(self):
        class Host:
            node = {"host": "entry.test"}

            def run(self, script, **kwargs):
                kwargs["stderr"].write(b"PRIVATE-CONTENT: no space left on device\n")
                raise subprocess.CalledProcessError(1, ["sensitive-command"])

        with self.assertRaises(WizardError) as caught, patch.dict(os.environ, {"ANET_PLAIN": "1"}):
            wizard.job(Host(), "runtime", "123", action="images", bundle="/private")
        self.assertIn("Освободите место", str(caught.exception))
        self.assertNotIn("PRIVATE-CONTENT", str(caught.exception))
        self.assertNotIn("sensitive-command", str(caught.exception))

    def test_reopen_existing_panel_without_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".local/state/anet-wizard/sample"
            directory.mkdir(parents=True)
            secret = directory / "password file"
            secret.write_text("never-display-this")
            spec = dict(installation="sample", local=0, panel=8093, password_file=str(secret),
                        servers=[dict(host="vpn.test", address="admin.test", port=2222, user="root")])
            (directory / "plan.json").write_text(json.dumps(spec))
            code, out = terminal_run([DEPLOY / "install.sh", "--access", "--plain"], extra_env={"HOME": tmp})
            self.assertEqual(code, 0)
            self.assertNotIn(b"never-display-this", out)
            self.assertIn(b"cat -- '", out)
            self.assertIn(b"-p 2222 -L 127.0.0.1:8093:127.0.0.1:8093 root@admin.test", out)
            self.assertIn(b"http://127.0.0.1:8093/", out)
            self.assertIn("Скачать профиль".encode(), out)

    def test_redirect_and_eof(self):
        p = subprocess.run([DEPLOY / "install.sh"], input=b"", capture_output=True)
        self.assertEqual(p.returncode, 1)
        self.assertNotIn(b"\033", p.stdout + p.stderr)
        self.assertIn("Поток ввода закрыт".encode(), p.stderr)


if __name__ == "__main__":
    unittest.main()
