import os
from pathlib import Path
import sys
import unittest

from test_wizard_terminal import terminal_run

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CLEAR = "\x1b[2J\x1b[H"
TRANSITIONS = """
from wizard_ui import Terminal
ui = Terminal()
ui.menu('Главное меню', [(1, 'Начать', '')], 1)
ui.stage(2, 'Проверка плана')
print('entry.test → exit.test', flush=True)
ui.menu('Применить этот план?', [(1, 'Да', '')], 1, new_screen=False)
ui.stage(6, 'Результат')
print('ssh -L 8088:127.0.0.1:8088 test@entry.test', flush=True)
"""


class WizardScreens(unittest.TestCase):
    def run_transitions(self, plain=False):
        code, raw = terminal_run([sys.executable, "-c", TRANSITIONS], b"\n\n",
                                 {"PYTHONPATH": os.fspath(SCRIPTS), "ANET_PLAIN": "1" if plain else "0"})
        self.assertEqual(code, 0)
        return raw.decode()

    def test_confirmation_shares_plan_and_access_stays_on_final_screen(self):
        screens = self.run_transitions().split(CLEAR)
        self.assertEqual(len(screens), 4)
        self.assertIn("Главное меню", screens[1])
        self.assertIn("╭", screens[1])
        self.assertIn("Этап 2/6", screens[2])
        self.assertIn("entry.test → exit.test", screens[2])
        self.assertIn("Применить этот план?", screens[2])
        self.assertIn("ssh -L", screens[3])

    def test_plain_transcript_and_prompt_errors_keep_context(self):
        output = self.run_transitions(plain=True)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("╭", output)
        self.assertIn("Главное меню", output)
        self.assertIn("entry.test → exit.test", output)
        self.assertIn("ssh -L", output)
