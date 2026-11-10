from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness.tools import execute_tool


class ToolTests(unittest.TestCase):
    def test_exit_at_shell_approval_prompt_pauses_before_running_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-run"
            with patch("builtins.input", return_value="/exit"), self.assertRaises(KeyboardInterrupt):
                execute_tool("run_command", {"command": f"touch {marker}"}, Path(directory))
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
