import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wizard_site import render_site


class SiteTests(unittest.TestCase):
    def test_local_pages_do_not_need_network(self):
        with patch("wizard_site.build_opener", side_effect=AssertionError):
            self.assertEqual(render_site("studio"), render_site("default"))
            for kind, title in (("studio", "Этюды формы"), ("journal", "Бумажные формы")):
                self.assertIn(f"<h1>{title}</h1>", render_site(kind))

    def test_github_page_escapes_public_fields_and_builds_link_from_login(self):
        payload = dict(login="Tester", name="<script>", location='A & B', bio="<img onerror=x>",
                       public_repos=0, html_url="javascript:alert(1)")
        with patch("wizard_site.build_opener") as factory:
            fetch = factory.return_value.open
            fetch.return_value = io.BytesIO(json.dumps(payload).encode())
            page = render_site("github", "tester")
            req = fetch.call_args.args[0]
            self.assertEqual(req.full_url, "https://api.github.com/users/tester")
            self.assertFalse(req.has_header("Authorization"))
            self.assertGreater(fetch.call_args.kwargs["timeout"], 0)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("A &amp; B", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("javascript:", page)
        self.assertIn('href="https://github.com/tester?tab=repositories"', page)

    def test_invalid_username_never_makes_a_request(self):
        with patch("wizard_site.build_opener", side_effect=AssertionError):
            for name in (".", "../repo", "ab--cd", "-login", "login-", "x" * 40, "", "https://example.org", "user\n"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "логин"):
                    render_site("github", name)

    def test_fetch_errors_are_actionable(self):
        for status, text in ((404, "не найден"), (403, "ограничил"), (429, "ограничил"), (500, "HTTP 500")):
            with self.subTest(status=status), patch("wizard_site.build_opener") as factory:
                factory.return_value.open.side_effect = HTTPError("", status, "", {}, None)
                with self.assertRaisesRegex(ValueError, text):
                    render_site("github", "tester")
        with patch("wizard_site.build_opener") as factory:
            factory.return_value.open.side_effect = TimeoutError
            with self.assertRaisesRegex(ValueError, "Проверьте сеть"):
                render_site("github", "tester")

    def test_invalid_payload(self):
        for raw in (b"?", b" " * 65537, b'{"login":"other"}', b'{"login":123}', b'{"login":"123","public_repos":false}'):
            with self.subTest(size=len(raw)), patch("wizard_site.build_opener") as factory:
                factory.return_value.open.return_value = io.BytesIO(raw)
                with self.assertRaises(ValueError):
                    render_site("github", "123")
