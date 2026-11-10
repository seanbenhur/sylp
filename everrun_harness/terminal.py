from __future__ import annotations

import builtins
import curses
import re
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_TRANSCRIPT_LINES = 1500
_RAIL_MIN_WIDTH = 100
_CHANGES_MIN_WIDTH = 90
_RAIL_WIDTH = 26
_CHANGES_WIDTH = 38
_PANEL_REFRESH_SECONDS = 2.0
_CHANGES_REFRESH_SECONDS = 3.0
_IDLE_REFRESH_MS = 1500
_STATUS_ICONS = {"running": "●", "waiting_for_input": "!", "paused": "❚", "review_required": "✓"}


def _acs(name: str, fallback: str) -> Any:
    # ACS constants only exist after initscr; tests draw without a real terminal.
    return getattr(curses, name, ord(fallback))


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
        self.repo: Path | None = None
        self.task_id: str | None = None
        self._list_sessions: Callable[[], list[dict[str, Any]]] | None = None
        self._diff_summary: Callable[[], str] | None = None
        self._panel_store: Any = None
        self.show_rail = True
        self.show_changes = False
        self._rail_cache: list[dict[str, Any]] = []
        self._rail_fetched = 0.0
        self._changes_cache: tuple[int, list[str]] = (0, [])
        self._changes_fetched = 0.0

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
            self._close_panel_store()

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

    def bind_session(self, repo: Path, task_id: str | None, *,
                     list_sessions: Callable[[], list[dict[str, Any]]] | None = None,
                     diff_summary: Callable[[], str] | None = None) -> None:
        """Wire the session rail and changes pane to live data for this repository.

        The default callables read the durable task store and the task worktree;
        tests and demos can inject fakes instead.
        """
        self.repo = Path(repo)
        self.task_id = task_id
        if list_sessions is None or diff_summary is None:
            if self._panel_store is None:
                from .store import Store
                self._panel_store = Store()
        if list_sessions is None:
            store = self._panel_store
            repo_key = str(self.repo.resolve())

            def list_sessions(store: Any = store, repo_key: str = repo_key) -> list[dict[str, Any]]:
                return [item for item in store.list()
                        if str(Path(item["repo"]).resolve()) == repo_key]
        if diff_summary is None and task_id is not None:
            store = self._panel_store

            def diff_summary(store: Any = store, task_id: str = task_id) -> str:
                task = store.get(task_id)
                if not task:
                    return "Session is no longer available."
                from .git import diff_summary as summarize
                return summarize(Path(task["worktree"]), task["base_commit"])
        self._list_sessions = list_sessions
        self._diff_summary = diff_summary
        self._rail_fetched = 0.0
        self._changes_fetched = 0.0
        self._dirty = True

    def _close_panel_store(self) -> None:
        if self._panel_store is not None:
            try:
                self._panel_store.close()
            except Exception:
                pass
            self._panel_store = None

    def _rail_entries(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._rail_cache and now - self._rail_fetched < _PANEL_REFRESH_SECONDS:
            return self._rail_cache
        entries: list[dict[str, Any]] = []
        if self._list_sessions is not None:
            try:
                sessions = self._list_sessions()
            except Exception:
                sessions = []
            for item in sessions[:9]:
                status = str(item.get("status") or "")
                entries.append({
                    "id": str(item.get("id") or ""),
                    "goal": str(item.get("goal") or ""),
                    "status": status,
                    "icon": _STATUS_ICONS.get(status, "·"),
                    "current": item.get("id") == self.task_id,
                })
        self._rail_cache = entries
        self._rail_fetched = now
        return entries

    def _attention_count(self) -> int:
        return sum(1 for entry in self._rail_entries()
                   if entry["status"] == "waiting_for_input" and not entry["current"])

    def _changes_lines(self, width: int) -> list[str]:
        now = time.monotonic()
        cached_width, cached_lines = self._changes_cache
        if cached_width == width and cached_lines and now - self._changes_fetched < _CHANGES_REFRESH_SECONDS:
            return cached_lines
        if self._diff_summary is None:
            text = ""
        else:
            try:
                text = self._diff_summary()
            except Exception as exc:
                text = f"Could not read changes: {exc}"
        lines: list[str] = []
        for raw in (text or "No changes yet.").splitlines():
            pieces = textwrap.wrap(raw.expandtabs(2), width=max(8, width - 3),
                                   replace_whitespace=False, drop_whitespace=False,
                                   break_long_words=True, break_on_hyphens=False)
            lines.extend(pieces or [""])
        self._changes_cache = (width, lines)
        self._changes_fetched = now
        return lines

    def _rail_width(self, width: int) -> int:
        if not self.show_rail or self._list_sessions is None or width < _RAIL_MIN_WIDTH:
            return 0
        return _RAIL_WIDTH

    def _changes_width(self, width: int) -> int:
        if not self.show_changes or self._diff_summary is None or width < _CHANGES_MIN_WIDTH:
            return 0
        return _CHANGES_WIDTH

    def _transcript_width(self) -> int:
        if self.screen is None:
            return 118
        width = self.screen.getmaxyx()[1]
        return max(20, width - self._rail_width(width) - self._changes_width(width) - 2)

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
        if self._list_sessions is not None and hasattr(self.screen, "timeout"):
            try:
                # Wake periodically so the session rail and changes pane pick up
                # other Everrun processes while the prompt sits idle.
                self.screen.timeout(_IDLE_REFRESH_MS)
            except curses.error:
                pass
        while True:
            self._draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                self._dirty = True
                continue
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
            elif key == curses.KEY_F2:
                self.show_rail = not self.show_rail
                self._dirty = True
            elif key == curses.KEY_F3:
                self.show_changes = not self.show_changes
                self._dirty = True
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
            line_width = self._transcript_width()
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

    def _draw_rail(self, screen: Any, height: int, rail_width: int) -> None:
        header_style = curses.A_BOLD | (curses.color_pair(1) if self._colors else 0)
        screen.addnstr(1, 0, " Sessions", rail_width - 2, header_style)
        entries = self._rail_entries()
        row = 2
        for entry in entries:
            if row + 1 >= height - 6:
                break
            if self._colors:
                color = {"running": 2, "waiting_for_input": 3, "review_required": 1}.get(entry["status"])
                entry_style = curses.color_pair(color) if color else curses.A_DIM
            else:
                entry_style = curses.A_NORMAL
            if entry["current"]:
                entry_style |= curses.A_BOLD
            marker = "▸" if entry["current"] else " "
            screen.addnstr(row, 0, f"{marker}{entry['icon']} {entry['id']}", rail_width - 2, entry_style)
            goal = textwrap.shorten(entry["goal"], width=max(8, rail_width - 5), placeholder="…")
            screen.addnstr(row + 1, 2, goal, rail_width - 4, curses.A_DIM)
            row += 2
        if not entries:
            screen.addnstr(2, 0, " (no saved sessions)", rail_width - 2, curses.A_DIM)
        for y in range(1, height - 2):
            screen.addch(y, rail_width - 1, _acs("ACS_VLINE", "|"))

    def _draw_changes(self, screen: Any, height: int, width: int, changes_width: int) -> None:
        x = width - changes_width
        header_style = curses.A_BOLD | (curses.color_pair(1) if self._colors else 0)
        screen.addnstr(1, x + 1, " Changes", changes_width - 2, header_style)
        lines = self._changes_lines(changes_width)
        for index, line in enumerate(lines[:max(0, height - 9)]):
            screen.addnstr(2 + index, x + 1, line, changes_width - 2)
        for y in range(1, height - 2):
            screen.addch(y, x, _acs("ACS_VLINE", "|"))

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
        rail_width = self._rail_width(width)
        changes_width = self._changes_width(width)
        text_x = rail_width
        text_width = max(1, width - rail_width - changes_width)
        header_style = curses.A_REVERSE | curses.A_BOLD
        if self._colors:
            header_style |= curses.color_pair(1)
        attention = self._attention_count() if self._list_sessions is not None else 0
        status_text = f"● {self.status}"
        if attention:
            status_text += f" · {attention} need input"
        title_width = max(1, width - len(status_text) - 4)
        screen.addnstr(0, 0, self.title, title_width, header_style)
        status_x = max(title_width + 2, width - len(status_text) - 2)
        if self._colors:
            status_style = curses.A_BOLD | curses.color_pair(4 if attention else 3)
        else:
            status_style = curses.A_BOLD
        screen.addnstr(0, status_x, status_text, max(0, width - status_x - 1), status_style)
        input_rows = 4
        body_height = height - input_rows - 3
        if rail_width:
            self._draw_rail(screen, height, rail_width)
        if changes_width:
            self._draw_changes(screen, height, width, changes_width)
        wrapped: list[tuple[str, int]] = []
        for line in self.lines:
            expanded = line.expandtabs(2)
            pieces = textwrap.wrap(expanded, width=max(1, text_width - 2), replace_whitespace=False,
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
                screen.addnstr(row, text_x, line, text_width - 1, style)
        screen.hline(height - input_rows - 2, 0, _acs("ACS_HLINE", "-"), width - 1)
        screen.addnstr(height - input_rows - 1, 0,
                       "Enter: send   Ctrl+O: new line   ↑/↓: move/history   F2: sessions   F3: changes   Ctrl+C or /exit: pause   /help: commands",
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
