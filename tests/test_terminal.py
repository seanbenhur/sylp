from __future__ import annotations

import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from everrun_harness.terminal import _MAX_TRANSCRIPT_LINES, TerminalUI


class FakeScreen:
    def __init__(self, height: int = 30, width: int = 120):
        self.height = height
        self.width = width
        self.calls: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        pass

    def refresh(self):
        pass

    def hline(self, *args):
        pass

    def addch(self, *args):
        pass

    def move(self, *args):
        pass

    def addnstr(self, y, x, text, n, *args):
        self.calls.append((y, x, text[:n] if n and n > 0 else text))


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

    def test_task_rail_marks_current_session_and_counts_attention(self):
        ui = TerminalUI("Everrun")
        ui.bind_session(Path("/repo"), "aaa", list_sessions=lambda: [
            {"id": "aaa", "goal": "Current task", "status": "running"},
            {"id": "bbb", "goal": "Waiting task", "status": "waiting_for_input"},
            {"id": "ccc", "goal": "Paused task", "status": "paused"},
        ], diff_summary=lambda: "No changes yet.")

        entries = ui._rail_entries()

        self.assertEqual([entry["id"] for entry in entries], ["aaa", "bbb", "ccc"])
        self.assertTrue(entries[0]["current"])
        self.assertEqual(entries[1]["icon"], "!")
        self.assertEqual(entries[2]["icon"], "❚")
        self.assertEqual(ui._attention_count(), 1)

    def test_panels_stay_off_without_session_binding(self):
        ui = TerminalUI("Everrun")

        self.assertEqual(ui._rail_entries(), [])
        self.assertEqual(ui._attention_count(), 0)
        self.assertEqual(ui._rail_width(120), 0)
        self.assertEqual(ui._changes_width(120), 0)
        self.assertEqual(ui._transcript_width(), 118)

    def test_changes_pane_wraps_diff_summary_to_panel_width(self):
        ui = TerminalUI("Everrun")
        ui.bind_session(Path("/repo"), "aaa", list_sessions=lambda: [],
                        diff_summary=lambda: " app.py | 5 +++--\n " + "x" * 100)

        self.assertFalse(ui.show_changes)
        ui.show_changes = True
        self.assertEqual(ui._changes_width(120), 38)
        self.assertEqual(ui._changes_width(80), 0)
        lines = ui._changes_lines(38)
        self.assertTrue(any("app.py" in line for line in lines))
        self.assertLessEqual(max(map(len, lines)), 35)

    def test_f2_and_f3_toggle_panels_from_the_prompt(self):
        keys = iter([curses.KEY_F2, curses.KEY_F3, "\x03"])

        class KeyScreen:
            def get_wch(self):
                return next(keys)

        ui = TerminalUI("Everrun")
        ui.screen = KeyScreen()
        ui.bind_session(Path("/repo"), "aaa", list_sessions=lambda: [], diff_summary=lambda: "")
        with patch.object(ui, "_draw"), patch.object(ui, "_record_input"):
            value = ui.input("You> ")

        self.assertEqual(value, "/exit")
        self.assertFalse(ui.show_rail)
        self.assertTrue(ui.show_changes)

    def test_draw_renders_rail_changes_and_attention_header(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.bind_session(Path("/repo"), "aaa", list_sessions=lambda: [
            {"id": "aaa", "goal": "Fix bug", "status": "running"},
            {"id": "bbb", "goal": "Docs", "status": "waiting_for_input"},
        ], diff_summary=lambda: " app.py | 5 +++--")
        ui.show_changes = True
        ui.lines = ["Everrun> hello"]

        ui._draw()

        rendered = "\n".join(text for _, _, text in ui.screen.calls)
        self.assertIn("Sessions", rendered)
        self.assertIn("aaa", rendered)
        self.assertIn("Changes", rendered)
        self.assertIn("app.py", rendered)
        self.assertIn("1 need input", rendered)
        transcript_x = [x for _, x, text in ui.screen.calls if "Everrun> hello" in text]
        self.assertTrue(transcript_x and transcript_x[0] >= 26)


    def test_tool_output_hidden_by_default_with_placeholder(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["Everrun> on it"]
        ui._tool_lines = [False]
        ui.write("\n[tool run_command running]\n")
        ui.write("building project\n")
        ui.write("done in 2s\n")
        ui.write("\n[tool run_command complete] done in 2s\n")
        ui.write("Everrun> build passed\n")

        ui._draw()

        rendered = "\n".join(text for _, _, text in ui.screen.calls)
        self.assertIn("Everrun> on it", rendered)
        self.assertIn("Everrun> build passed", rendered)
        self.assertNotIn("building project", rendered)
        self.assertNotIn("[tool run_command running]", rendered)
        self.assertIn("tool lines hidden · F4 to show", rendered)

    def test_f4_toggle_reveals_tool_output(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.write("\n[tool read_file]\nfile contents here\n")

        ui._draw()
        hidden = "\n".join(text for _, _, text in ui.screen.calls)
        self.assertNotIn("file contents here", hidden)

        ui.screen.calls.clear()
        ui.show_tool_calls = True
        ui._draw()
        shown = "\n".join(text for _, _, text in ui.screen.calls)
        self.assertIn("[tool read_file]", shown)
        self.assertIn("file contents here", shown)
        self.assertNotIn("hidden · F4", shown)

    def test_f4_toggles_tool_calls_from_the_prompt(self):
        keys = iter([curses.KEY_F4, "\x03"])

        class KeyScreen:
            def get_wch(self):
                return next(keys)

        ui = TerminalUI("Everrun")
        ui.screen = KeyScreen()
        self.assertFalse(ui.show_tool_calls)
        with patch.object(ui, "_draw"), patch.object(ui, "_record_input"):
            value = ui.input("You> ")

        self.assertEqual(value, "/exit")
        self.assertTrue(ui.show_tool_calls)

    def test_regular_lines_still_render_around_hidden_tool_output(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["Everrun> first"]
        ui._tool_lines = [False]
        ui.write("\n[tool run_command]\noutput\n")
        ui.write("Everrun> second\n")

        ui._draw()

        rendered = "\n".join(text for _, _, text in ui.screen.calls)
        self.assertIn("Everrun> first", rendered)
        self.assertIn("Everrun> second", rendered)
        self.assertNotIn("output", rendered)


if __name__ == "__main__":
    unittest.main()
