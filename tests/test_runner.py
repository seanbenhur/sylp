from __future__ import annotations

import contextlib
import io
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from everrun_harness import runner
from everrun_harness.git import prepare_worktree
from everrun_harness.store import Store


class RunnerIntegrationTests(unittest.TestCase):
    def test_task_lock_prevents_two_processes_from_running_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            ready = root / "worker-ready"
            release = root / "worker-release"
            script = textwrap.dedent("""
                import sys
                import time
                from pathlib import Path
                from everrun_harness.runner import _task_lock

                ready, release = map(Path, sys.argv[1:])
                with _task_lock("lock-test"):
                    ready.write_text("ready", encoding="utf-8")
                    deadline = time.monotonic() + 10
                    while not release.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    if not release.exists():
                        raise SystemExit("test parent did not release the worker")
            """)
            with patch.dict(os.environ, {"EVERUN_HOME": str(state)}):
                worker = subprocess.Popen([sys.executable, "-c", script, str(ready), str(release)],
                                          cwd=Path(__file__).resolve().parents[1],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), worker.stderr.read() if worker.poll() is not None else "")
                    with self.assertRaisesRegex(RuntimeError, "already open in another Everrun process"), \
                            runner._task_lock("lock-test"):
                        pass
                    release.write_text("release", encoding="utf-8")
                    stdout, stderr = worker.communicate(timeout=10)
                    self.assertEqual(worker.returncode, 0, stderr or stdout)
                    with runner._task_lock("lock-test"):
                        pass
                finally:
                    release.touch()
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=5)

    def test_task_executes_tool_persists_result_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Everrun Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "everrun-test@example.invalid"], cwd=root, check=True)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)

            state = Path(directory) / "state"
            with patch.dict(os.environ, {"EVERUN_HOME": str(state)}):
                worktree, base = prepare_worktree(root, "integration1")
                store = Store()
                task_id = store.create(goal="List the source files and report.", acceptance=[],
                                       repo=root, worktree=worktree, base_commit=base,
                                       model="fake-model", provider="openai", auto_approve=True)
                responses = iter([
                    {"text": "", "tool_calls": [{"id": "call1", "name": "list_files",
                                                     "arguments": {"path": "."}}],
                     "history_items": [{"type": "function_call", "call_id": "call1",
                                        "name": "list_files", "arguments": '{"path":"."}'}],
                     "response_id": "response1"},
                    {"text": "The repository contains app.py.", "tool_calls": [],
                     "history_items": [{"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text",
                                                     "text": "The repository contains app.py."}]}],
                     "response_id": "response2", "usage": {"input_tokens": 12, "output_tokens": 7}},
                ])
                statuses = []
                with patch.object(runner, "complete", side_effect=lambda *args, **kwargs: next(responses)), \
                        contextlib.redirect_stdout(io.StringIO()):
                    runner.run_task(task_id, on_status=statuses.append)

                final = store.get(task_id)
                events = store.events(task_id)
                messages = store.messages(task_id)
                store.close()

            self.assertEqual(final["status"], "review_required")
            self.assertIn("Thinking · turn 1", statuses)
            self.assertIn("Running list_files", statuses)
            self.assertEqual(statuses[-1], "Review required")
            self.assertTrue(any(event["kind"] == "tool_finished" for event in events))
            tool_result = next(message["output"] for message in messages
                               if message.get("type") == "function_call_output")
            self.assertIn("app.py", tool_result)
            self.assertNotIn(".git", tool_result)
            report = state / "tasks" / task_id / "report.md"
            self.assertIn("The repository contains app.py.", report.read_text(encoding="utf-8"))
            self.assertIn("Input: 12; output: 7", report.read_text(encoding="utf-8"))

    def test_process_restart_recovers_tool_without_replaying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Everrun Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "everrun-test@example.invalid"], cwd=root, check=True)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)

            state = Path(directory) / "state"
            worktree, base = prepare_worktree(root, "recovery001")
            store = Store(state / "state.sqlite3")
            task_id = store.create(goal="Continue the command safely.", acceptance=[], repo=root,
                                   worktree=worktree, base_commit=base, model="fake-model", provider="openai")
            store.add_message(task_id, "openai", {
                "type": "function_call", "call_id": "call-restart", "name": "run_command",
                "arguments": '{"command":"touch must-not-run"}',
            })
            store.event(task_id, "tool_started", {"call_id": "call-restart", "name": "run_command"})
            store.event(task_id, "tool_output", {"call_id": "call-restart", "chunk": "partial command output"})
            store.close()

            child_script = textwrap.dedent("""
                import sys
                from everrun_harness import runner

                def complete(provider, model, history, tools, system):
                    recovered = [item for item in history if item.get("type") == "function_call_output"]
                    assert recovered and "partial command output" in recovered[-1]["output"]
                    assert "inspect the current repository" in recovered[-1]["output"]
                    return {
                        "text": "Recovered safely after restart.",
                        "tool_calls": [],
                        "history_items": [{"type": "message", "role": "assistant",
                                           "content": [{"type": "output_text",
                                                        "text": "Recovered safely after restart."}]}],
                    }

                runner.complete = complete
                runner.run_task(sys.argv[1])
            """)
            env = {**os.environ, "EVERUN_HOME": str(state)}
            child = subprocess.run([sys.executable, "-c", child_script, task_id], cwd=root,
                                   env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr or child.stdout)

            reopened = Store(state / "state.sqlite3")
            final = reopened.get(task_id)
            events = reopened.events(task_id)
            reopened.close()
            self.assertEqual(final["status"], "review_required")
            self.assertTrue(any(event["kind"] == "tool_recovered_after_interruption" for event in events))
            self.assertFalse((worktree / "must-not-run").exists())

    def test_hard_kill_during_shell_tool_then_resume_without_duplicate_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Everrun Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "everrun-test@example.invalid"], cwd=root, check=True)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)

            state = Path(directory) / "state"
            worktree, base = prepare_worktree(root, "hardkill01")
            store = Store(state / "state.sqlite3")
            task_id = store.create(goal="Run one long shell task and finish.", acceptance=[], repo=root,
                                   worktree=worktree, base_commit=base, model="fake-model", provider="openai",
                                   auto_approve=True)
            store.close()

            side_effect = Path(directory) / "side-effects.txt"
            tool_pid = Path(directory) / "tool.pid"
            command = (f"printf 'ran\\n' >> {shlex.quote(str(side_effect))}; "
                       "printf 'started\\n'; "
                       f"echo $$ > {shlex.quote(str(tool_pid))}; "
                       "while :; do sleep 1; done")
            start_script = textwrap.dedent(f"""
                from everrun_harness import runner

                def complete(provider, model, history, tools, system):
                    return {{
                        "text": "", "tool_calls": [{{"id": "call-hardkill", "name": "run_command",
                                                        "arguments": {{"command": {command!r}}}}}],
                        "history_items": [{{"type": "function_call", "call_id": "call-hardkill",
                                           "name": "run_command", "arguments": {{"command": {command!r}}}}}],
                    }}

                runner.complete = complete
                runner.run_task({task_id!r})
            """)
            env = {**os.environ, "EVERUN_HOME": str(state)}
            worker = subprocess.Popen([sys.executable, "-c", start_script], cwd=root, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            shell_group = None
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        stderr = worker.stderr.read() if worker.stderr else ""
                        self.fail(f"Everrun exited before the test interrupted it: {stderr}")
                    if side_effect.exists() and tool_pid.exists():
                        try:
                            shell_group = int(tool_pid.read_text(encoding="utf-8").strip())
                        except ValueError:
                            shell_group = None
                    if shell_group:
                        db = sqlite3.connect(state / "state.sqlite3")
                        rows = db.execute("SELECT kind,payload FROM events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
                        db.close()
                        kinds = [kind for kind, _payload in rows]
                        if "tool_started" in kinds and "tool_output" in kinds:
                            break
                    time.sleep(0.05)
                else:
                    db = sqlite3.connect(state / "state.sqlite3")
                    rows = db.execute("SELECT kind,payload FROM events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
                    task_status = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                    db.close()
                    self.fail(f"Everrun did not reach a durable in-progress shell tool; "
                              f"worker={worker.poll()}, side_effect={side_effect.exists()}, "
                              f"tool_pid={tool_pid.exists()}, status={task_status}, events={rows!r}")

                os.kill(worker.pid, signal.SIGKILL)
                worker.wait(timeout=5)
                try:
                    os.killpg(shell_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass

                resume_script = textwrap.dedent("""
                    import sys
                    from everrun_harness import runner

                    def complete(provider, model, history, tools, system):
                        recovered = [item for item in history if item.get("type") == "function_call_output"]
                        assert recovered and "started" in recovered[-1]["output"]
                        assert "Do not repeat the action" in recovered[-1]["output"]
                        return {
                            "text": "Recovered safely.", "tool_calls": [],
                            "history_items": [{"type": "message", "role": "assistant",
                                               "content": [{"type": "output_text", "text": "Recovered safely."}]}],
                        }

                    runner.complete = complete
                    runner.run_task(sys.argv[1])
                """)
                resumed = subprocess.run([sys.executable, "-c", resume_script, task_id], cwd=root,
                                         env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(resumed.returncode, 0, resumed.stderr or resumed.stdout)
                self.assertEqual(side_effect.read_text(encoding="utf-8").splitlines(), ["ran"])
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=5)
                if shell_group:
                    try:
                        os.killpg(shell_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if worker.stderr:
                    worker.stderr.close()


class SessionSwitchTests(unittest.TestCase):
    def _repo_with_two_sessions(self, directory: str):
        root = Path(directory) / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Everrun Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "everrun-test@example.invalid"], cwd=root, check=True)
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)
        store = Store()
        worktree, base = prepare_worktree(root, "switchsrc1")
        task_id = store.create(goal="First session", acceptance=[], repo=root, worktree=worktree,
                               base_commit=base, model="fake-model", provider="openai")
        worktree2, base2 = prepare_worktree(root, "switchdst1")
        target_id = store.create(goal="Second session", acceptance=[], repo=root, worktree=worktree2,
                                 base_commit=base2, model="fake-model", provider="openai")
        store.update(target_id, status="paused")
        return root, store, task_id, target_id

    def test_switch_command_pauses_current_session_and_returns_target(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with patch.dict(os.environ, {"EVERUN_HOME": str(state)}):
                root, store, task_id, target_id = self._repo_with_two_sessions(directory)
                responses = iter([{"text": "Done so far.", "tool_calls": [],
                                   "history_items": [{"type": "message", "role": "assistant",
                                                      "content": [{"type": "output_text",
                                                                   "text": "Done so far."}]}]}])
                with patch.object(runner, "complete", side_effect=lambda *args, **kwargs: next(responses)), \
                        patch("builtins.input", return_value=f"/switch {target_id}"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = runner.run_task(task_id, interactive=True)
                final = store.get(task_id)
                events = store.events(task_id)
                store.close()

            self.assertEqual(result, target_id)
            self.assertEqual(final["status"], "paused")
            self.assertTrue(any(event["kind"] == "session_switched" and f'"to": "{target_id}"' in event["payload"]
                                for event in events))

    def test_switch_lists_sessions_and_rejects_bad_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with patch.dict(os.environ, {"EVERUN_HOME": str(state)}):
                root, store, task_id, target_id = self._repo_with_two_sessions(directory)
                task = store.get(task_id)
                with patch("builtins.input",
                           side_effect=["/switch", "/switch nope", f"/switch {task_id}", "/exit"]), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    value = runner._read_session_message(task, store, "openai")
                store.close()

            self.assertEqual(value, "/exit")
            printed = out.getvalue()
            self.assertIn("Paused sessions you can switch to:", printed)
            self.assertIn(target_id, printed)
            self.assertIn("No session 'nope' exists for this repository.", printed)
            self.assertIn("already in this session", printed)

    def test_switch_rejects_sessions_that_are_not_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with patch.dict(os.environ, {"EVERUN_HOME": str(state)}):
                root, store, task_id, target_id = self._repo_with_two_sessions(directory)
                store.update(target_id, status="running")
                task = store.get(task_id)
                with patch("builtins.input", side_effect=[f"/switch {target_id}", "/exit"]), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    value = runner._read_session_message(task, store, "openai")
                store.close()

            self.assertEqual(value, "/exit")
            self.assertIn("switch to a paused or waiting session instead", out.getvalue())


if __name__ == "__main__":
    unittest.main()
