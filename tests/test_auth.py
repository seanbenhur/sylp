from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness import auth, providers


class AuthTests(unittest.TestCase):
    def test_fresh_oauth_access_token_does_not_need_a_refresh_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "auth.json").write_text(json.dumps({"openai": {
                "auth_type": "oauth", "access_token": "fresh-token",
                "refresh_token": "refresh-token", "expires_at": time.time() + 600,
            }}), encoding="utf-8")
            original_open = Path.open

            def deny_lock_file(path, *args, **kwargs):
                if path.name == "auth.lock":
                    raise PermissionError("refresh lock should not be needed")
                return original_open(path, *args, **kwargs)

            with patch.object(auth, "config_home", return_value=home), \
                    patch.object(providers, "config_home", return_value=home), \
                    patch.object(Path, "open", deny_lock_file):
                token = auth.openai_access_token()

        self.assertEqual(token, "fresh-token")

    def test_antigravity_fresh_oauth_token_returned_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "auth.json").write_text(json.dumps({"antigravity": {
                "auth_type": "oauth", "access_token": "ag-fresh-token",
                "refresh_token": "ag-refresh-token", "expires_at": time.time() + 600,
            }}), encoding="utf-8")

            with patch.object(auth, "config_home", return_value=home), \
                    patch.object(providers, "config_home", return_value=home):
                token = auth.antigravity_access_token()

        self.assertEqual(token, "ag-fresh-token")

    def test_antigravity_login_imports_existing_ide_token(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_gemini_token = home / "jetski-token"
            fake_gemini_token.write_text(json.dumps({
                "token": {"refresh_token": "mock-rt"}
            }), encoding="utf-8")

            with patch.object(auth, "config_home", return_value=home), \
                    patch.object(providers, "config_home", return_value=home), \
                    patch.object(auth, "_antigravity_token_file", return_value=fake_gemini_token), \
                    patch.object(auth, "_refresh_google_token", return_value={"access_token": "refreshed-tok", "expires_in": 3600}), \
                    patch.object(auth, "get_antigravity_email", return_value="developer@example.com"):
                auth.login_antigravity()
                saved = providers.load_auth().get("antigravity", {})

            self.assertEqual(saved.get("access_token"), "refreshed-tok")
            self.assertEqual(saved.get("email"), "developer@example.com")
            self.assertEqual(saved.get("source"), "antigravity_ide")


if __name__ == "__main__":
    unittest.main()
