import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wizard
import wizard_edit as edit


def legacy():
    names = ("entry.test", "west.test", "east.test")
    return dict(installation="0123456789", local=0, base=43999, panel=8088,
                servers=[dict(address=name, host=name, port=22, local=i == 0, user="deploy")
                         for i, name in enumerate(names)],
                profiles=[dict(entry=0, exit=i, protocol="anet") for i in (1, 2)])


class PlanEditor(unittest.TestCase):
    def edit_node(self, spec, index, path, answers):
        ui = Mock()
        ui.menu.side_effect = answers
        with contextlib.redirect_stdout(io.StringIO()):
            return edit.edit_plan(spec, path, index, ui=ui, ask=Mock(), number=Mock())

    def test_older_plan_skip_restore_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            spec = legacy()
            before = json.dumps(spec)
            path.write_text(before)
            self.assertFalse(self.edit_node(spec, 1, path, [2, 0]))
            self.assertEqual(path.read_text(), before)
            self.assertEqual(spec, json.loads(before))
            self.assertTrue(self.edit_node(spec, 1, path, [2, 4]))
            self.assertEqual([p["slot"] for p in edit.active_profiles(spec)], [2])
            self.assertEqual(spec["panel_native_port"], 45000)
            hist, = (path.parent / spec["installation"] / "plan-history").glob("*.json")
            self.assertEqual(hist.read_text(), before)
            self.assertEqual(hist.stat().st_mode & 0o777, 0o600)
            self.assertTrue(self.edit_node(spec, 1, path, [2, 4]))
            self.assertEqual([p["slot"] for p in edit.active_profiles(spec)], [1, 2])

    def test_removal_is_local_and_last_route_can_be_saved_as_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            spec = edit.normalize(legacy())
            self.edit_node(spec, 1, path, [3, 1, 4])
            self.assertEqual(spec["servers"][1]["status"], "removed")
            self.assertEqual(spec["profiles"][1]["exit"], 2)
            self.edit_node(spec, 2, path, [2, 4])
            self.assertEqual(edit.active_profiles(spec), [])
            with self.assertRaisesRegex(ValueError, "Нет действующего маршрута"):
                edit.validate(spec, for_apply=True)
            self.edit_node(spec, 2, path, [2, 4])
            self.assertEqual([p["slot"] for p in edit.active_profiles(spec)], [2])

    def test_completed_plan_can_be_reopened_without_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".local/state/anet-wizard"
            state.mkdir(mode=0o700, parents=True)
            spec = edit.normalize(legacy())
            spec["servers"][1]["status"] = "skipped"
            installed = state / spec["installation"] / "plan.json"
            installed.parent.mkdir(mode=0o700)
            original = json.dumps(spec)
            installed.write_text(original)
            run = subprocess.run([Path(wizard.__file__).parents[1] / "install.sh", "--plain"],
                                 input="4\n1\n2\n2\n4\n0\n", env=dict(os.environ, HOME=tmp),
                                 capture_output=True, timeout=5, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Затронутые профили", run.stdout)
            self.assertEqual(installed.read_text(), original)
            draft = json.loads((state / "pending.json").read_text())
            self.assertEqual(draft["servers"][1]["status"], "active")
            self.assertEqual([p["slot"] for p in edit.active_profiles(draft)], [1, 2])

    def test_duplicate_ssh_address_can_be_repaired_in_old_pending_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = legacy()
            spec["servers"][1]["address"] = spec["servers"][2]["address"]
            pending = Path(tmp) / "pending.json"
            pending.write_text(json.dumps(spec))
            ui = Mock()
            ui.menu.side_effect = [1, 2, 4]
            ask = Mock(side_effect=["west.test", "new-ssh.test", "deploy"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(edit.edit_plan(spec, pending, 1, ui=ui, ask=ask, number=lambda *args: 2222))
            self.assertEqual(spec["servers"][1]["port"], 2222)
            self.assertTrue(spec["servers"][1]["password"])
            self.assertEqual(spec["servers"][1]["address"], "new-ssh.test")

    def test_invalid_topologies(self):
        valid = edit.normalize(legacy())
        for change in (lambda p: p["servers"][0].update(status="removed"),
                       lambda p: p["profiles"][0].update(exit=8),
                       lambda p: p["profiles"][0].update(entry=1),
                       lambda p: p["profiles"][1].update(slot=1),
                       lambda p: p["servers"][2].update(address="west.test"),
                       lambda p: p.update(installation="../outside")):
            spec = copy.deepcopy(valid)
            change(spec)
            with self.assertRaises(ValueError):
                edit.validate(spec)

    def test_apply_skips_ssh_and_preserves_project_ports_and_catalog_ids(self):
        spec = edit.normalize(legacy())
        spec["servers"][1]["status"] = "skipped"
        opened = []
        jobs = []

        class Host:
            def __init__(self, node, socket):
                self.node = node
                opened.append(node["host"])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def run(self, cmd, **opts):
                if "input" in opts:
                    jobs.append(json.loads(opts["input"]))
                    output = b'{"config":"fixture","public_key":"public-fixture","password_file":"/test/password"}'
                else:
                    output = self.node["host"].encode()
                return subprocess.CompletedProcess([], 0, output)

        with tempfile.TemporaryDirectory() as tmp, patch.object(wizard, "STATE", Path(tmp)), \
             patch.object(wizard, "Host", Host), patch.object(wizard, "prepare"), \
             patch.object(wizard, "sources"), contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            wizard.apply(spec, Path(tmp))
            self.assertTrue((Path(tmp) / spec["installation"] / "2-anet.toml").is_file())
            spec["servers"][2]["user"] = "other"
            with self.assertRaisesRegex(wizard.WizardError, "учётной записи Docker"):
                wizard.apply(spec, Path(tmp))
        self.assertEqual(opened, ["entry.test", "east.test"] * 2)
        self.assertEqual([j["name"] for j in jobs], ["exit-2", "entry-2", "panel"])
        self.assertEqual([j["native_port"] for j in jobs], [44000, 44000, 45000])
        self.assertEqual(jobs[-1]["catalog"][0]["id"], "profile-2")
        self.assertEqual(jobs[-1]["catalog"][0]["exit_public_key"], "public-fixture")
        self.assertEqual(jobs[-1]["exits"], [dict(address="east.test", quic_port=44000, ssh_port=22,
                                                  ssh_user="deploy", public_key="public-fixture")])


if __name__ == "__main__":
    unittest.main()
