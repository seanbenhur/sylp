from __future__ import annotations

import json
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from sylp_harness.store import Store

ROOT = Path(__file__).resolve().parents[1]


_FAKE_PROVIDER_SITE = r'''
import json
import os
import sys
import time
from pathlib import Path
from sylp_harness import providers

trace_path = Path(os.environ["SYLP_FAKE_PROVIDER_TRACE"])
call_count = 0

def complete(provider, model, messages, tools, system, *, stream_output=True):
    global call_count
    call_count += 1
    with trace_path.open("a", encoding="utf-8") as trace:
        trace.write(json.dumps({"provider": provider, "model": model,
                                "history_length": len(messages)}) + "\n")
    if call_count == 1:
        call = {"id": "call-list-files", "type": "function",
                "function": {"name": "list_files", "arguments": '{"path":"."}'}}
        return {"text": "", "tool_calls": [{"id": call["id"], "name": "list_files",
                                                  "arguments": {"path": "."}}],
                "assistant_message": {"role": "assistant", "content": None,
                                      "tool_calls": [call]},
                "finish_reason": "tool_calls", "streamed": False}
    text = "The local model streamed this reply."
    if stream_output:
        for chunk in ("The local model ", "streamed this reply."):
            sys.stdout.write(chunk)
            sys.stdout.flush()
            time.sleep(0.06)
    return {"text": text, "tool_calls": [],
            "assistant_message": {"role": "assistant", "content": text},
            "finish_reason": "stop", "streamed": stream_output}

providers.complete = complete
'''


class FullTUIProviderIntegrationTests(unittest.TestCase):
    def test_fullscreen_chat_streams_tools_and_resumes_after_process_restart(self):
        executable = shutil.which("sylp", path=str(ROOT / ".venv" / "bin"))
        if not executable:
            self.skipTest("editable Sylp console script is not installed in .venv")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                           capture_output=True, timeout=10)
            subprocess.run(["git", "config", "user.name", "Sylp Integration"], cwd=repo,
                           check=True, capture_output=True, timeout=10)
            subprocess.run(["git", "config", "user.email", "sylp@example.invalid"], cwd=repo,
                           check=True, capture_output=True, timeout=10)
            (repo / "hello.py").write_text("print('hello')\n", encoding="utf-8")
            subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True,
                           capture_output=True, timeout=10)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True,
                           capture_output=True, timeout=10)

            helper = base / "provider-hook"
            helper.mkdir()
            (helper / "sitecustomize.py").write_text(_FAKE_PROVIDER_SITE, encoding="utf-8")
            state = base / "sylp-state"
            trace_path = base / "provider-trace.jsonl"
            env = {
                **os.environ,
                "SYLP_HOME": str(state),
                "SYLP_BASE_URL": "http://provider-disabled.invalid/v1",
                "SYLP_PROVIDER": "openai-compatible",
                "SYLP_MODEL": "local-test-model",
                "SYLP_FAKE_PROVIDER_TRACE": str(trace_path),
                "PYTHONPATH": os.pathsep.join((str(helper), str(ROOT), os.environ.get("PYTHONPATH", ""))),
                "TERM": "xterm-256color",
            }
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                [executable], cwd=repo, env=env, stdin=slave_fd, stdout=slave_fd,
                stderr=slave_fd, close_fds=True, start_new_session=True,
            )
            os.close(slave_fd)
            transcript = bytearray()
            try:
                self._read_until(process, master_fd, transcript, "❯ ".encode("utf-8"), timeout=15)
                os.write(master_fd, b"hello\r")
                self._read_until(process, master_fd, transcript, b"streamed this reply.", timeout=20)
                self._read_until(process, master_fd, transcript, b"Waiting for your message", timeout=5)
                # Tool output hides behind the F4 toggle by default; reveal it
                # so the streamed file listing shows up in the transcript.
                # xterm-256color terminfo maps F4 to SS3 S (\x1bOS).
                os.write(master_fd, b"\x1bOS")
                self._read_until(process, master_fd, transcript, b"hello.py", timeout=5)
                time.sleep(0.2)
                os.write(master_fd, b"/exit\r")
                self._read_until(process, master_fd, transcript, b"Session paused. Resume with:", timeout=5)

                store = Store(state / "state.sqlite3")
                tasks = store.list()
                self.assertEqual(len(tasks), 1)
                task = store.get(tasks[0]["id"])
                messages = store.messages(task["id"])
                events = store.events(task["id"])
                store.close()

                rendered = transcript.decode("utf-8", errors="replace")
                self.assertIn("The local model", rendered)
                self.assertIn("hello.py", rendered)
                self.assertEqual(task["status"], "paused")
                provider_calls = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(provider_calls), 2)
                self.assertEqual({call["provider"] for call in provider_calls}, {"openai-compatible"})
                self.assertEqual({call["model"] for call in provider_calls}, {"local-test-model"})
                self.assertTrue(any(message.get("role") == "tool" or
                                    message.get("type") == "function_call_output" for message in messages))
                self.assertTrue(any(event["kind"] == "session_paused" for event in events))

                # Simulate closing the first app process and opening the saved
                # task in a fresh Sylp process. The first process may remain
                # inside curses teardown on this synthetic PTY, so terminate it
                # after the durable pause has been verified.
                process.kill()
                process.wait(timeout=5)

                resumed_master, resumed_slave = pty.openpty()
                resumed = subprocess.Popen(
                    [executable, "resume", task["id"]], cwd=repo, env=env,
                    stdin=resumed_slave, stdout=resumed_slave, stderr=resumed_slave,
                    close_fds=True, start_new_session=True,
                )
                os.close(resumed_slave)
                resumed_transcript = bytearray()
                try:
                    self._read_until(resumed, resumed_master, resumed_transcript,
                                     b"Resuming session. Previous status: paused", timeout=15)
                    time.sleep(0.2)
                    os.write(resumed_master, b"continue\r")
                    self._read_until(resumed, resumed_master, resumed_transcript,
                                     b"The local model streamed this reply.", timeout=20)
                    time.sleep(0.2)
                    os.write(resumed_master, b"/exit\r")
                    self._read_until(resumed, resumed_master, resumed_transcript,
                                     b"Session paused. Resume with:", timeout=5)
                finally:
                    if resumed.poll() is None:
                        resumed.kill()
                        resumed.wait(timeout=5)
                    os.close(resumed_master)

                store = Store(state / "state.sqlite3")
                resumed_task = store.get(task["id"])
                resumed_messages = store.messages(task["id"])
                resumed_events = store.events(task["id"])
                store.close()
                self.assertEqual(resumed_task["status"], "paused")
                self.assertTrue(any(message.get("role") == "user" and message.get("content") == "continue"
                                    for message in resumed_messages))
                self.assertGreaterEqual(sum(event["kind"] == "session_paused" for event in resumed_events), 2)
                provider_calls = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(provider_calls), 4)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master_fd)

    @staticmethod
    def _read_until(process: subprocess.Popen[bytes], fd: int, transcript: bytearray,
                    needle: bytes, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while needle not in transcript and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            transcript.extend(chunk)
        if needle not in transcript:
            output = transcript.decode("utf-8", errors="replace")
            raise AssertionError(f"Timed out waiting for {needle!r}. Transcript tail:\n{output[-3000:]}")

if __name__ == "__main__":
    unittest.main()
