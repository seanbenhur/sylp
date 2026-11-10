from __future__ import annotations

import builtins
import curses
import re
import sys
import textwrap
import threading
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

# Composer chrome: top separator, input rows, bottom separator with the key
# hints, and a dim status row, like the framed prompt in Claude Code.
_INPUT_ROWS = 4
_COMPOSER_ROWS = _INPUT_ROWS + 3
_HINTS = " ⏎ send · ^O line · ↑↓ history · F2 sessions · F3 changes · F4 tools · /help "

# Statuses where the agent is not actively working; anything else gets the
# animated spinner and elapsed timer in the header (codex's "Working (3s)").
_IDLE_STATUSES = {
    "Ready",
    "Paused",
    "Paused after error",
    "Review required",
    "Waiting for your message",
    "Waiting for your answer",
    "Approval needed",
}
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_SECONDS = 0.15

_MD_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`\n]+`)")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_LABEL = re.compile(r"^(You|user|Model ID|Approval)>\s?(.*)$", re.S)
_YN = re.compile(r"^\[y/N\] (.*)$", re.S)


def _acs(name: str, fallback: str) -> Any:
    # ACS constants only exist after initscr; tests draw without a real terminal.
    return getattr(curses, name, ord(fallback))


def format_elapsed(seconds: float) -> str:
    """Compact elapsed timer, codex style: 12s, then 4m 05s."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60:02d}s"


def md_spans(text: str, base: str = "text") -> list[tuple[str, str]]:
    """Split a prose line into (text, style) spans, honoring **bold** and `code`."""
    spans: list[tuple[str, str]] = []
    pos = 0
    for match in _MD_INLINE.finditer(text):
        if match.start() > pos:
            spans.append((text[pos:match.start()], base))
        token = match.group(0)
        if token.startswith("**"):
            spans.append((token[2:-2], "bold"))
        else:
            spans.append((token[1:-1], "code"))
        pos = match.end()
    if pos < len(text):
        spans.append((text[pos:], base))
    return spans


def wrap_spans(spans: list[tuple[str, str]], width: int) -> list[list[tuple[str, str]]]:
    """Greedy word wrap over styled characters; returns rows of (char, style)."""
    chars: list[tuple[str, str]] = []
    for text, key in spans:
        chars.extend((ch, key) for ch in text)
    if not chars:
        return [[]]
    width = max(1, width)
    rows: list[list[tuple[str, str]]] = []
    index = 0
    total = len(chars)
    while index < total:
        take = chars[index:index + width]
        index += len(take)
        if index < total and len(take) == width:
            boundary = next((j for j in range(len(take) - 1, 0, -1) if take[j][0] == " "), None)
            if boundary is not None:
                take = take[:boundary + 1]
                index -= width - boundary - 1
        rows.append(take)
    return rows


class TerminalUI:
    """Small full-screen terminal shell for Everrun's persistent chat loop."""

    def __init__(self, title: str):
        self.title = title
        self.status = "Ready"
        self._status_started = time.monotonic()
        self.screen: Any = None
        self.lines = ["Type /help for commands.", ""]
        # Parallel to self.lines: True for lines that belong to a tool block
        # (the [tool ...] markers plus any streamed tool output between them).
        self._tool_lines = [False, False]
        # Tool activity hides behind F4 so the transcript reads like a chat.
        self.show_tool_calls = False
        self._in_tool_stream = False
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
        self._draw_lock = threading.Lock()
        self._ticker_stop: threading.Event | None = None
        self._ticker: threading.Thread | None = None
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
        self._wrap_cache_key: tuple[int, int, int, bool] | None = None
        self._wrap_cache: list[tuple[list[tuple[str, str]], bool]] = []

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
        self._ticker_stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, daemon=True, name="everrun-tui-ticker")
        self._ticker.start()
        try:
            callback()
        finally:
            self._ticker_stop.set()
            self._ticker.join(timeout=1.0)
            self._ticker = None
            self._ticker_stop = None

    def _tick(self) -> None:
        # Animate the header spinner and elapsed timer while the agent works.
        # Draws already happen from the input loop and writer path, so the
        # ticker only fires for busy statuses; the draw lock serializes curses.
        assert self._ticker_stop is not None
        while not self._ticker_stop.wait(_TICK_SECONDS):
            if self.screen is None or self.status in _IDLE_STATUSES:
                continue
            self._dirty = True
            try:
                self._draw()
            except Exception:
                pass

    def write(self, value: str) -> int:
        clean = _ANSI.sub("", value).replace("\r", "")
        tool = self._classify_tool_write(clean)
        old_line_count = len(self.lines)
        self._append_text(clean, tool=tool)
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
        if status != self.status:
            self._status_started = time.monotonic()
        self.status = status
        self._dirty = True
        self._draw()

    def header_status(self, now: float | None = None) -> tuple[str, bool]:
        """Header status text plus whether the agent is busy.

        Busy statuses get an animated braille spinner and an elapsed timer,
        mirroring codex's "Working (4m 55s)" and Claude Code's spinner row.
        """
        now = time.monotonic() if now is None else now
        if self.status in _IDLE_STATUSES:
            return f"● {self.status}", False
        frame = _SPINNER_FRAMES[int((now - self._status_started) / _TICK_SECONDS) % len(_SPINNER_FRAMES)]
        text = f"{frame} {self.status}"
        elapsed = now - self._status_started
        if elapsed >= 1:
            text += f" · {format_elapsed(elapsed)}"
        return text, True

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
            self._tool_lines.extend([False] * (len(prompt_lines) - 1))
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
            elif key == curses.KEY_F4:
                self.show_tool_calls = not self.show_tool_calls
                self._dirty = True
            elif isinstance(key, str) and key.isprintable():
                self.buffer = self.buffer[:self.cursor_pos] + key + self.buffer[self.cursor_pos:]
                self.cursor_pos += len(key)
                history_position = None

    def _classify_tool_write(self, clean: str) -> bool:
        # The runner prints each tool marker as one write, and streamed tool
        # output as chunks between the "running" and "complete" markers.
        first = next((line for line in clean.split("\n") if line.strip()), "")
        if first.startswith("[tool "):
            self._in_tool_stream = first.endswith(" running]")
            return True
        return self._in_tool_stream

    def _record_input(self, value: str) -> None:
        parts = value.split("\n")
        self._append_line()
        marker = "You> " if self.prompt.rstrip() in {"You>", "You"} else self.prompt
        self._append_text(f"{marker}{parts[0]}\n")
        for part in parts[1:]:
            self._append_text(f"  {part}\n")
        self._append_text("\n")

    def _append_line(self, tool: bool = False) -> None:
        self.lines.append("")
        if len(self._tool_lines) < len(self.lines):
            self._tool_lines.append(tool)

    def _append_text(self, value: str, tool: bool = False) -> None:
        parts = value.split("\n")
        for index, segment in enumerate(parts):
            line_width = self._transcript_width()
            while segment:
                available = line_width - len(self.lines[-1])
                if available <= 0:
                    self._append_line(tool)
                    available = line_width
                if not self.lines[-1] and self._tool_lines:
                    self._tool_lines[-1] = tool
                self.lines[-1] += segment[:available]
                segment = segment[available:]
                if segment:
                    self._append_line(tool)
            if index < len(parts) - 1:
                self._append_line(tool)
        if len(self.lines) > _MAX_TRANSCRIPT_LINES:
            removed = len(self.lines) - _MAX_TRANSCRIPT_LINES
            del self.lines[:removed]
            del self._tool_lines[:removed]
            self.scroll = min(self.scroll, len(self.lines) - 1)

    def _move_vertical(self, direction: int) -> None:
        before = self.buffer[:self.cursor_pos]
        current_line = before.count("\n")
        column = len(before.rsplit("\n", 1)[-1])
        lines = self.buffer.split("\n")
        target_line = max(0, min(len(lines) - 1, current_line + direction))
        self.cursor_pos = sum(len(lines[index]) + 1 for index in range(target_line))
        self.cursor_pos += min(column, len(lines[target_line]))

    def _style(self, key: str) -> int:
        colors = getattr(self, "_colors", False)
        if key == "user_marker":
            return curses.A_BOLD | (curses.color_pair(2) if colors else 0)
        if key == "assistant_marker":
            return curses.A_BOLD | (curses.color_pair(1) if colors else 0)
        if key in {"bold", "heading"}:
            return curses.A_BOLD
        if key == "code":
            return curses.A_REVERSE | (curses.A_BOLD if colors else 0)
        if key == "code_block":
            return curses.A_DIM
        if key == "tool":
            return (curses.color_pair(3) if colors else 0) | curses.A_DIM
        if key == "approval":
            return curses.A_BOLD | (curses.color_pair(3) if colors else 0)
        if key == "error":
            return curses.A_BOLD | (curses.color_pair(4) if colors else 0)
        if key in {"dim", "placeholder"}:
            return curses.A_DIM
        return curses.A_NORMAL

    def _line_spans(self, line: str, is_tool: bool, in_code: bool) -> list[tuple[str, str]]:
        """Turn one stored transcript line into styled spans for drawing.

        Stored text is unchanged; rendering adds the chat chrome: ❯ for the
        user, • for the assistant, markdown for prose, dim indents for code.
        """
        if is_tool or line.startswith("[tool"):
            return [(line, "tool")]
        yn = _YN.match(line)
        if yn:
            return [("❯ ", "user_marker"), (yn.group(1), "text")]
        if line.startswith("Everrun> ") or line.startswith("assistant> "):
            rest = line.split("> ", 1)[1]
            return [("• ", "assistant_marker")] + self._prose_spans(rest, in_code)
        label = _LABEL.match(line)
        if label:
            return [("❯ ", "user_marker"), (label.group(2), "text")]
        if line.startswith("Allow command"):
            return [(line, "approval")]
        if line.startswith("everrun:"):
            return [(line, "error")]
        return self._prose_spans(line, in_code)

    def _prose_spans(self, text: str, in_code: bool) -> list[tuple[str, str]]:
        if text.strip().startswith("```"):
            return [(text, "dim")]
        if in_code:
            return [("  " + text, "code_block")] if text else []
        heading = _MD_HEADING.match(text)
        if heading:
            return [(heading.group(1), "heading")]
        return md_spans(text)

    def _draw_rail(self, screen: Any, height: int, rail_width: int) -> None:
        composer_top = height - _COMPOSER_ROWS
        header_style = curses.A_BOLD | (curses.color_pair(1) if self._colors else 0)
        screen.addnstr(1, 0, " Sessions", rail_width - 2, header_style)
        entries = self._rail_entries()
        row = 2
        for entry in entries:
            if row + 1 >= composer_top:
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
        for y in range(1, composer_top):
            screen.addch(y, rail_width - 1, _acs("ACS_VLINE", "|"))

    def _draw_changes(self, screen: Any, height: int, width: int, changes_width: int) -> None:
        composer_top = height - _COMPOSER_ROWS
        x = width - changes_width
        header_style = curses.A_BOLD | (curses.color_pair(1) if self._colors else 0)
        screen.addnstr(1, x + 1, " Changes", changes_width - 2, header_style)
        lines = self._changes_lines(changes_width)
        for index, line in enumerate(lines[:max(0, composer_top - 2)]):
            screen.addnstr(2 + index, x + 1, line, changes_width - 2)
        for y in range(1, composer_top):
            screen.addch(y, x, _acs("ACS_VLINE", "|"))

    def _draw_composer(self, screen: Any, height: int, width: int) -> None:
        """The framed input area: separators, ❯ prompt, hints, status row."""
        top = height - _COMPOSER_ROWS
        hline = _acs("ACS_HLINE", "-")
        screen.hline(top, 0, hline, width - 1)
        hint_row = top + _INPUT_ROWS + 1
        screen.hline(hint_row, 0, hline, width - 1)
        if width > len(_HINTS) + 2:
            screen.addnstr(hint_row, width - len(_HINTS) - 1, _HINTS, len(_HINTS), curses.A_DIM)

        # Dim status row under the composer, codex style: where this session lives.
        status_row = top + _INPUT_ROWS + 2
        footer = f" {self.repo}" if self.repo else ""
        if self.scroll:
            footer += "  ·  scrolled up - PgDn returns to the latest"
        if footer:
            screen.addnstr(status_row, 0, footer, width - 1, curses.A_DIM)

        # Input rows come last so the cursor lands in the composer.
        if self.prompt.rstrip() in {"You>", "You"}:
            marker, label = "❯ ", ""
        else:
            marker, label = "❯ ", self.prompt
        label_style = curses.A_BOLD
        if self._colors and label.startswith("["):
            label_style |= curses.color_pair(3)
        logical_lines = self.buffer.split("\n")
        cursor_prefix = self.buffer[:self.cursor_pos]
        cursor_line = cursor_prefix.count("\n")
        cursor_column = len(cursor_prefix.rsplit("\n", 1)[-1])
        first_visible = min(max(0, len(logical_lines) - _INPUT_ROWS),
                            max(0, cursor_line - _INPUT_ROWS + 1))
        for visible_index, line_index in enumerate(range(first_visible, len(logical_lines))):
            row = top + 1 + visible_index
            if line_index == 0:
                screen.addnstr(row, 0, marker, len(marker), self._style("user_marker"))
                if label:
                    screen.addnstr(row, len(marker), label, len(label), label_style)
                prefix = marker + label
            else:
                prefix = "  "
            if line_index == cursor_line:
                # Horizontal scroll keeps the prompt marker pinned on the left.
                avail = max(1, width - 1 - len(prefix))
                text = logical_lines[line_index]
                text_start = max(0, cursor_column - avail + 1)
                screen.addnstr(row, len(prefix), text[text_start:], avail, curses.A_BOLD)
                screen.move(row, min(len(prefix) + cursor_column - text_start, width - 2))
            else:
                screen.addnstr(row, len(prefix), logical_lines[line_index], width - 1 - len(prefix))

    def _draw(self) -> None:
        with self._draw_lock:
            self._draw_locked()

    def _draw_locked(self) -> None:
        if self.screen is None:
            return
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()
        if height < _COMPOSER_ROWS + 3 or width < 20:
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
        status_text, busy = self.header_status()
        if attention:
            status_text += f" · {attention} need input"
        title_width = max(1, width - len(status_text) - 4)
        screen.addnstr(0, 0, self.title, title_width, header_style)
        status_x = max(title_width + 2, width - len(status_text) - 2)
        if self._colors:
            status_style = curses.A_BOLD | curses.color_pair(4 if attention else (3 if busy else 2))
        else:
            status_style = curses.A_BOLD
        screen.addnstr(0, status_x, status_text, max(0, width - status_x - 1), status_style)
        composer_top = height - _COMPOSER_ROWS
        body_height = composer_top - 1
        if rail_width:
            self._draw_rail(screen, height, rail_width)
        if changes_width:
            self._draw_changes(screen, height, width, changes_width)
        wrapped = self._wrapped_rows(text_width)
        self.scroll = max(0, min(self.scroll, max(0, len(wrapped) - 1)))
        visible = wrapped[-(body_height + self.scroll):-self.scroll if self.scroll else None]
        start = max(1, body_height - len(visible) + 1)
        for index, (row, continuation) in enumerate(visible[-body_height:]):
            row_y = start + index
            if row_y >= composer_top:
                break
            indent = 2 if continuation else 0
            x = text_x + indent
            merged: list[list[Any]] = []
            for ch, style_key in row:
                if merged and merged[-1][1] == style_key:
                    merged[-1][0] += ch
                else:
                    merged.append([ch, style_key])
            for text_piece, style_key in merged:
                if not text_piece:
                    continue
                screen.addnstr(row_y, x, text_piece, max(0, text_x + text_width - 1 - x),
                               self._style(style_key))
                x += len(text_piece)
                if x >= text_x + text_width - 1:
                    break
        self._draw_composer(screen, height, width)
        screen.refresh()
        self._last_draw = time.monotonic()
        self._dirty = False

    def _wrapped_rows(self, text_width: int) -> list[tuple[list[tuple[str, str]], bool]]:
        """Styled, wrapped transcript rows, cached between draws.

        The ticker redraws several times a second while the spinner runs, so
        re-wrapping only happens when the transcript, width, or tool
        visibility actually changes.
        """
        cache_key = (text_width, len(self.lines), len(self.lines[-1]) if self.lines else 0,
                     self.show_tool_calls)
        if cache_key == self._wrap_cache_key:
            return self._wrap_cache
        wrapped: list[tuple[list[tuple[str, str]], bool]] = []
        hidden_tool_lines = 0

        def flush_hidden() -> None:
            nonlocal hidden_tool_lines
            if hidden_tool_lines:
                noun = "line" if hidden_tool_lines == 1 else "lines"
                text = f"[{hidden_tool_lines} tool {noun} hidden · F4 to show]"
                wrapped.append(([(ch, "placeholder") for ch in text], False))
                hidden_tool_lines = 0

        in_code = False
        for index, line in enumerate(self.lines):
            is_tool = bool(line) and index < len(self._tool_lines) and self._tool_lines[index]
            if is_tool and not self.show_tool_calls:
                hidden_tool_lines += 1
                continue
            flush_hidden()
            spans = self._line_spans(line.expandtabs(2), is_tool, in_code)
            if not is_tool and line.strip().startswith("```"):
                in_code = not in_code
            for row_index, row in enumerate(wrap_spans(spans, max(1, text_width - 2))):
                wrapped.append((row, row_index > 0))
        flush_hidden()
        self._wrap_cache_key = cache_key
        self._wrap_cache = wrapped
        return wrapped
