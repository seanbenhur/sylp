from __future__ import annotations

import unittest
from unittest.mock import patch

from everrun_harness.terminal import _MAX_TRANSCRIPT_LINES, TerminalUI


class TerminalTests(unittest.TestCase):
    def test_status_updates_are_kept_for_header_rendering(self):
        ui = TerminalUI("Everrun")
        with patch.object(ui, "_draw") as draw:
            ui.set_status("Thinking · turn 2")

        self.assertEqual(ui.status, "Thinking · turn 2")
        draw.assert_called_once_with()

    def test_run_routes_print_and_input_to_full_screen_ui(self):
        ui = TerminalUI("Everrun")
        received = []

        def callback():
            print("Session output")
            received.append(input("You> "))

        with patch("everrun_harness.terminal.curses.wrapper", side_effect=lambda _run, fn: fn()), \
                patch.object(ui, "input", return_value="hello") as ui_input:
            ui.run(callback)

        self.assertEqual(received, ["hello"])
        ui_input.assert_called_once_with("You> ")
        self.assertIn("Session output", "\n".join(ui.lines))

    def test_ctrl_c_immediately_returns_pause_command(self):
        class KeyScreen:
            def get_wch(self):
                return "\x03"

        ui = TerminalUI("Everrun")
        ui.screen = KeyScreen()
        with patch.object(ui, "_draw"), patch.object(ui, "_record_input") as record_input:
            value = ui.input("Approval> ")

        self.assertEqual(value, "/exit")
        record_input.assert_called_once_with("/exit")

    def test_streamed_output_is_wrapped_and_scrollback_is_bounded(self):
        ui = TerminalUI("Everrun")
        ui.write("x" * 250_000)

        self.assertLessEqual(len(ui.lines), _MAX_TRANSCRIPT_LINES)
        self.assertLessEqual(max(map(len, ui.lines)), 118)

    def test_streaming_output_preserves_scrollback_position(self):
        ui = TerminalUI("Everrun")
        ui.lines = [f"line {index}" for index in range(50)]
        ui.scroll = 10

        ui.write("\nnew output")

        self.assertEqual(ui.scroll, 11)


if __name__ == "__main__":
    unittest.main()
