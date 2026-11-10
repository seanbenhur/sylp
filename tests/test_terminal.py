from __future__ import annotations

import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from everrun_harness.terminal import (
    _MAX_TRANSCRIPT_LINES,
    _SPINNER_FRAMES,
    TerminalUI,
    format_elapsed,
    wrap_spans,
)


class FakeScreen:
    def __init__(self, height: int = 30, width: int = 120):
        self.height = height
        self.width = width
        self.calls: list[tuple[int, int, str, int]] = []

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
        attr = args[0] if args else 0
        self.calls.append((y, x, text[:n] if n and n > 0 else text, attr))


def rendered_text(screen: FakeScreen) -> str:
    return "\n".join(text for _, _, text, _ in screen.calls)


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

        rendered = rendered_text(ui.screen)
        self.assertIn("Sessions", rendered)
        self.assertIn("aaa", rendered)
        self.assertIn("Changes", rendered)
        self.assertIn("app.py", rendered)
        self.assertIn("1 need input", rendered)
        transcript_x = [x for _, x, text, _ in ui.screen.calls if "hello" in text]
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

        rendered = rendered_text(ui.screen)
        self.assertIn("on it", rendered)
        self.assertIn("build passed", rendered)
        self.assertNotIn("building project", rendered)
        self.assertNotIn("[tool run_command running]", rendered)
        self.assertIn("tool lines hidden · F4 to show", rendered)

    def test_f4_toggle_reveals_tool_output(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.write("\n[tool read_file]\nfile contents here\n")

        ui._draw()
        hidden = rendered_text(ui.screen)
        self.assertNotIn("file contents here", hidden)

        ui.screen.calls.clear()
        ui.show_tool_calls = True
        ui._draw()
        shown = rendered_text(ui.screen)
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

        rendered = rendered_text(ui.screen)
        self.assertIn("first", rendered)
        self.assertIn("second", rendered)
        self.assertNotIn("output", rendered)

    def test_chat_view_does_not_render_speaker_names(self):
        ui = TerminalUI("repo · provider/model")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["Type /help for commands.", "You> fix the parser", "Everrun> done"]
        ui._tool_lines = [False, False, False]

        ui._draw()

        rendered = rendered_text(ui.screen)
        self.assertIn("Type /help for commands.", rendered)
        self.assertNotIn("Everrun", rendered)
        self.assertNotIn("You>", rendered)
        self.assertIn("❯", rendered)
        self.assertIn("•", rendered)

    def test_chat_prefixes_render_as_markers(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["You> fix the parser", "Everrun> sure, looking now"]
        ui._tool_lines = [False, False]

        ui._draw()

        rendered = rendered_text(ui.screen)
        self.assertIn("❯", rendered)
        self.assertIn("•", rendered)
        self.assertIn("fix the parser", rendered)
        self.assertIn("sure, looking now", rendered)
        self.assertNotIn("You> fix", rendered)
        self.assertNotIn("Everrun> sure", rendered)

    def test_markdown_bold_code_and_headings_get_styles(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["Everrun> use **pytest** and `uv run` next", "## Summary"]
        ui._tool_lines = [False, False]

        ui._draw()

        rendered = rendered_text(ui.screen)
        self.assertNotIn("**", rendered.replace("** ", ""))  # markers are stripped
        bold_calls = [text for _, _, text, attr in ui.screen.calls if attr & curses.A_BOLD]
        code_calls = [text for _, _, text, attr in ui.screen.calls if attr & curses.A_REVERSE]
        self.assertIn("pytest", bold_calls)
        self.assertIn("uv run", code_calls)
        self.assertIn("Summary", bold_calls)

    def test_code_fences_render_dim_and_toggle(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["Everrun> here:", "```python", "print(1)", "```", "done"]
        ui._tool_lines = [False] * 5

        ui._draw()

        by_text = {text: attr for _, _, text, attr in ui.screen.calls}
        self.assertTrue(by_text["  print(1)"] & curses.A_DIM)
        self.assertEqual(by_text["done"], curses.A_NORMAL)
        self.assertTrue(by_text["```python"] & curses.A_DIM)

    def test_busy_status_shows_spinner_and_elapsed_timer(self):
        ui = TerminalUI("Everrun")
        ui.status = "Thinking · turn 1"
        ui._status_started = 100.0

        text, busy = ui.header_status(now=165.0)

        self.assertTrue(busy)
        self.assertIn(text[0], _SPINNER_FRAMES)
        self.assertIn("Thinking · turn 1", text)
        self.assertIn("1m 05s", text)

    def test_idle_status_shows_static_dot_without_timer(self):
        ui = TerminalUI("Everrun")
        ui.status = "Waiting for your message"
        ui._status_started = 100.0

        text, busy = ui.header_status(now=200.0)

        self.assertFalse(busy)
        self.assertEqual(text, "● Waiting for your message")

    def test_format_elapsed_compact_style(self):
        self.assertEqual(format_elapsed(0.4), "0s")
        self.assertEqual(format_elapsed(12), "12s")
        self.assertEqual(format_elapsed(295), "4m 55s")

    def test_wrap_spans_preserves_text_and_styles(self):
        rows = wrap_spans([("hello ", "text"), ("brave", "bold"), (" world", "text")], 9)

        self.assertEqual("".join(ch for row in rows for ch, _ in row), "hello brave world")
        styled = {"".join(ch for ch, key in row if key == "bold") for row in rows} - {""}
        self.assertEqual(styled, {"brave"})

    def test_wrap_spans_hard_breaks_long_words(self):
        rows = wrap_spans([("abcdefghij", "text")], 4)

        self.assertEqual(["".join(ch for ch, _ in row) for row in rows], ["abcd", "efgh", "ij"])

    def test_composer_shows_marker_hints_and_status_row(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.bind_session(Path("/repo"), "aaa", list_sessions=lambda: [], diff_summary=lambda: "")

        ui._draw()

        top = 30 - 7
        prompt_calls = [(y, text) for y, _, text, _ in ui.screen.calls if text == "❯ "]
        self.assertIn((top + 1, "❯ "), prompt_calls)
        hints = [text for y, _, text, _ in ui.screen.calls if y == top + 5]
        self.assertTrue(any("F2 sessions" in text and "F4 tools" in text for text in hints))
        footer = [text for y, _, text, _ in ui.screen.calls if y == top + 6]
        self.assertTrue(any("/repo" in text for text in footer))

    def test_default_prompt_shows_only_the_marker(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.prompt = "You>"  # rstripped by input()

        ui._draw()

        top = 30 - 7
        composer_text = [text for y, _, text, _ in ui.screen.calls if y == top + 1 and text]
        self.assertEqual(composer_text, ["❯ "])
        ui._record_input("hello")
        self.assertIn("You> hello", ui.lines)

    def test_approval_prompt_and_lines_are_highlighted(self):
        ui = TerminalUI("Everrun")
        ui.screen = FakeScreen()
        ui._colors = False
        ui.lines = ["Allow command in task worktree?", "  ls -la"]
        ui._tool_lines = [False, False]
        ui.prompt = "[y/N] "

        ui._draw()

        by_text = {text: attr for _, _, text, attr in ui.screen.calls}
        self.assertTrue(by_text["Allow command in task worktree?"] & curses.A_BOLD)
        self.assertIn("[y/N] ", by_text)

    def test_multiline_prompt_keeps_line_flags_aligned(self):
        class KeyScreen:
            def getmaxyx(self):
                return (30, 120)

            def get_wch(self):
                return "\x03"

        ui = TerminalUI("Everrun")
        ui.screen = KeyScreen()
        with patch.object(ui, "_draw"):
            ui.input("Allow command in task worktree?\n  ls -la\n[y/N] ")

        self.assertEqual(len(ui.lines), len(ui._tool_lines))
        self.assertEqual(ui.prompt, "[y/N] ")


if __name__ == "__main__":
    unittest.main()
