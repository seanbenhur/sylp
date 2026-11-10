from __future__ import annotations

import builtins
import curses
import re
import sys
import textwrap
import time
from collections.abc import Callable
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_TRANSCRIPT_LINES = 1500


class TerminalUI:
    """Small full-screen terminal shell for Everrun's persistent chat loop."""

    def __init__(self, title: str):
        self.title = title
        self.status = "Ready"
        self.screen: Any = None
        self.lines = ["Everrun is ready. Type /help for commands.", ""]
        self.buffer = ""
        self.prompt = "You> "
        self.cursor_pos = 0
        self.scroll = 0
        self.old_stdout: Any = None
        self.old_stderr: Any = None
        self.old_input: Any = None
        self.input_history: list[str] = []
        self._last_draw = 0.0
        self._dirty = True

    def run(self, callback: Callable[[], None]) -> None:
        self.old_stdout, self.old_stderr, self.old_input = sys.stdout, sys.stderr, builtins.input
        sys.stdout = self
        sys.stderr = self
        builtins.input = self.input
        try:
            curses.wrapper(self._run, callback)
        finally:
            sys.stdout = self.old_stdout
            sys.stderr = self.old_stderr
            builtins.input = self.old_input

    def _run(self, screen: Any, callback: Callable[[], None]) -> None:
        self.screen = screen
        screen.keypad(True)
        self._colors = False
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            self._colors = True
        except curses.error:
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self._draw()
        callback()

    def write(self, value: str) -> int:
        clean = _ANSI.sub("", value).replace("\r", "")
        old_line_count = len(self.lines)
        self._append_text(clean)
        added_lines = max(0, len(self.lines) - old_line_count)
        if self.scroll:
            self.scroll = min(len(self.lines) - 1, self.scroll + added_lines)
        self._dirty = True
        if "\n" in clean or time.monotonic() - self._last_draw >= 0.05:
            self._draw()
        return len(value)

    def flush(self) -> None:
        if self._dirty and time.monotonic() - self._last_draw >= 0.05:
            self._draw()

    def set_status(self, status: str) -> None:
        self.status = status
        self._dirty = True
        self._draw()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.old_stdout.fileno()

    def input(self, prompt: str = "") -> str:
        prompt = _ANSI.sub("", prompt).rstrip()
        prompt_lines = prompt.splitlines()
        if len(prompt_lines) > 1:
            self.lines.extend(prompt_lines[:-1])
            self.prompt = prompt_lines[-1] + " "
        else:
            self.prompt = prompt or "You> "
        self.buffer = ""
        self.cursor_pos = 0
        history_position: int | None = None
        draft = ""
        self.scroll = 0
        while True:
            self._draw()
            try:
                key = self.screen.get_wch()
            except KeyboardInterrupt:
                value = "/exit"
                self._record_input(value)
                return value
            if key in ("\n", "\r", curses.KEY_ENTER):
                value = self.buffer
                self._record_input(value)
                if value.strip() and (not self.input_history or self.input_history[-1] != value):
                    self.input_history.append(value)
                self.input_history = self.input_history[-100:]
                self.buffer = ""
                self.cursor_pos = 0
                self._draw()
                return value
            if key == curses.KEY_UP:
                if "\n" in self.buffer and self.buffer[:self.cursor_pos].count("\n") > 0:
                    self._move_vertical(-1)
                    continue
                if self.input_history:
                    if history_position is None:
                        draft = self.buffer
                        history_position = len(self.input_history)
                    history_position = max(0, history_position - 1)
                    self.buffer = self.input_history[history_position]
                    self.cursor_pos = len(self.buffer)
                    continue
            if key == curses.KEY_DOWN:
                if "\n" in self.buffer and self.buffer[:self.cursor_pos].count("\n") < self.buffer.count("\n"):
                    self._move_vertical(1)
                    continue
                if history_position is not None:
                    history_position = min(len(self.input_history), history_position + 1)
                    self.buffer = draft if history_position == len(self.input_history) else self.input_history[history_position]
                    self.cursor_pos = len(self.buffer)
                    continue
            if key in ("\x03",):
                value = "/exit"
                self._record_input(value)
                return value
            if key in ("\x04",):
                value = "/exit"
                self._record_input(value)
                return value
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if self.cursor_pos:
                    self.buffer = self.buffer[:self.cursor_pos - 1] + self.buffer[self.cursor_pos:]
                    self.cursor_pos -= 1
            elif key == curses.KEY_DC:
                if self.cursor_pos < len(self.buffer):
                    self.buffer = self.buffer[:self.cursor_pos] + self.buffer[self.cursor_pos + 1:]
            elif key == curses.KEY_LEFT:
                self.cursor_pos = max(0, self.cursor_pos - 1)
            elif key == curses.KEY_RIGHT:
                self.cursor_pos = min(len(self.buffer), self.cursor_pos + 1)
            elif key in (curses.KEY_HOME, "\x01"):
                line_start = self.buffer.rfind("\n", 0, self.cursor_pos) + 1
                self.cursor_pos = line_start
            elif key in (curses.KEY_END, "\x05"):
                line_end = self.buffer.find("\n", self.cursor_pos)
                self.cursor_pos = len(self.buffer) if line_end < 0 else line_end
            elif key == "\x0f":
                self.buffer = self.buffer[:self.cursor_pos] + "\n" + self.buffer[self.cursor_pos:]
                self.cursor_pos += 1
            elif key == curses.KEY_PPAGE:
                self.scroll = min(max(0, len(self.lines) - 1), self.scroll + 10)
            elif key == curses.KEY_NPAGE:
                self.scroll = max(0, self.scroll - 10)
            elif isinstance(key, str) and key.isprintable():
                self.buffer = self.buffer[:self.cursor_pos] + key + self.buffer[self.cursor_pos:]
                self.cursor_pos += len(key)
                history_position = None

    def _record_input(self, value: str) -> None:
        parts = value.split("\n")
        self.lines.append("")
        self._append_text(f"{self.prompt}{parts[0]}\n")
        for part in parts[1:]:
            self._append_text(f"  {part}\n")
        self._append_text("\n")

    def _append_text(self, value: str) -> None:
        parts = value.split("\n")
        for index, segment in enumerate(parts):
            line_width = max(20, self.screen.getmaxyx()[1] - 2) if self.screen is not None else 118
            while segment:
                available = line_width - len(self.lines[-1])
                if available <= 0:
                    self.lines.append("")
                    available = line_width
                self.lines[-1] += segment[:available]
                segment = segment[available:]
                if segment:
                    self.lines.append("")
            if index < len(parts) - 1:
                self.lines.append("")
        if len(self.lines) > _MAX_TRANSCRIPT_LINES:
            removed = len(self.lines) - _MAX_TRANSCRIPT_LINES
            del self.lines[:removed]
            self.scroll = min(self.scroll, len(self.lines) - 1)

    def _move_vertical(self, direction: int) -> None:
        before = self.buffer[:self.cursor_pos]
        current_line = before.count("\n")
        column = len(before.rsplit("\n", 1)[-1])
        lines = self.buffer.split("\n")
        target_line = max(0, min(len(lines) - 1, current_line + direction))
        self.cursor_pos = sum(len(lines[index]) + 1 for index in range(target_line))
        self.cursor_pos += min(column, len(lines[target_line]))

    def _draw(self) -> None:
        if self.screen is None:
            return
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 10 or width < 20:
            screen.addnstr(0, 0, "Resize terminal to at least 20x10", max(0, width - 1))
            screen.refresh()
            return
        header_style = curses.A_REVERSE | curses.A_BOLD
        if self._colors:
            header_style |= curses.color_pair(1)
        status_text = f"● {self.status}"
        title_width = max(1, width - len(status_text) - 4)
        screen.addnstr(0, 0, self.title, title_width, header_style)
        status_x = max(title_width + 2, width - len(status_text) - 2)
        status_style = curses.A_BOLD | (curses.color_pair(3) if self._colors else 0)
        screen.addnstr(0, status_x, status_text, max(0, width - status_x - 1), status_style)
        input_rows = 4
        body_height = height - input_rows - 3
        wrapped: list[tuple[str, int]] = []
        for line in self.lines:
            expanded = line.expandtabs(2)
            pieces = textwrap.wrap(expanded, width=max(1, width - 2), replace_whitespace=False,
                                   drop_whitespace=False, break_long_words=True, break_on_hyphens=False)
            style = curses.A_NORMAL
            if line.startswith("You>"):
                style = curses.A_BOLD | (curses.color_pair(2) if self._colors else 0)
            elif line.startswith("Everrun>"):
                style = curses.A_BOLD | (curses.color_pair(1) if self._colors else 0)
            elif line.startswith("[tool"):
                style = curses.A_BOLD | (curses.color_pair(3) if self._colors else 0)
            elif line.startswith("everrun:"):
                style = curses.A_BOLD | (curses.color_pair(4) if self._colors else 0)
            wrapped.extend((piece, style) for piece in pieces)
            if not pieces:
                wrapped.append(("", style))
        visible = wrapped[-(body_height + self.scroll):-self.scroll if self.scroll else None]
        start = max(1, body_height - len(visible) + 1)
        for index, (line, style) in enumerate(visible[-body_height:]):
            row = start + index
            if row < height - 2:
                screen.addnstr(row, 0, line, width - 1, style)
        screen.hline(height - input_rows - 2, 0, curses.ACS_HLINE, width - 1)
        screen.addnstr(height - input_rows - 1, 0,
                       "Enter: send   Ctrl+O: new line   ↑/↓: move/history   Ctrl+C or /exit: pause   /help: commands",
                       width - 1, curses.A_DIM)
        logical_lines = self.buffer.split("\n")
        cursor_prefix = self.buffer[:self.cursor_pos]
        cursor_line = cursor_prefix.count("\n")
        cursor_column = len(cursor_prefix.rsplit("\n", 1)[-1])
        first_visible = min(max(0, len(logical_lines) - input_rows),
                            max(0, cursor_line - input_rows + 1))
        for visible_index, line_index in enumerate(range(first_visible, len(logical_lines))):
            row = height - input_rows + visible_index
            prefix = self.prompt if line_index == 0 else "  "
            current = prefix + logical_lines[line_index]
            if line_index == cursor_line:
                cursor = len(prefix) + cursor_column
                start = max(0, cursor - width + 2)
                screen.addnstr(row, 0, current[start:], width - 1, curses.A_BOLD)
                screen.move(row, min(max(0, cursor - start), width - 2))
            else:
                screen.addnstr(row, 0, current, width - 1)
        screen.refresh()
        self._last_draw = time.monotonic()
        self._dirty = False
