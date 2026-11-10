from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness import runner
from sylp_harness.store import Store


class DurableStoreTests(unittest.TestCase):
    def test_files_and_diff_commands_stay_inside_the_interactive_prompt(self):
        commands = []

        def execute(name, arguments, root):
            commands.append((name, arguments, root))
            return f"{name} output"

        task = {"id": "task", "status": "paused", "model": "model",
                "worktree": "/tmp/worktree"}
        with patch.object(runner, "_session_input", side_effect=["/files", "/diff", "continue"]), \
                patch.object(runner, "execute_tool", side_effect=execute):
            value = runner._read_session_message(task, object(), "openai")

        self.assertEqual(value, "continue")
        self.assertEqual([command[0] for command in commands],
                         ["list_files", "git_status", "git_diff"])

    def test_interactive_compact_is_a_command_not_a_model_message(self):
        original_history = [{"role": "assistant", "content": "status"}]
        compacted_history = [{"role": "user", "content": "checkpoint"}]
        statuses = []
        with patch.object(runner, "_read_session_message", side_effect=["/compact", "continue"]), \
                patch.object(runner, "_compact_history", return_value=compacted_history) as compact:
            value, history = runner._read_interactive_message(
                "task", {"model": "model"}, "openai", original_history, object(),
                on_status=statuses.append,
            )

        self.assertEqual(value, "continue")
        self.assertEqual(history, compacted_history)
        self.assertEqual(compact.call_args.kwargs, {"force": True, "on_status": statuses.append})
        self.assertEqual(statuses, ["Compacting context", "Waiting for your message"])

    def test_forced_compaction_persists_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            task_id = store.create(goal="checkpoint", acceptance=[], repo=Path(directory),
                                   worktree=Path(directory), base_commit="base", model="model")
            history = [{"role": "user", "content": "preserve this task context"}]
            store.add_message(task_id, "openai", history[0])
            statuses = []
            with patch.object(runner, "complete", return_value={"text": "Goal and progress checkpoint."}) as complete:
                compacted = runner._compact_history(
                    task_id, {"model": "model"}, "openai", history, store, force=True,
                    on_status=statuses.append,
                )

            self.assertEqual(compacted[0]["role"], "user")
            self.assertIn("Goal and progress checkpoint.", compacted[0]["content"])
            self.assertEqual(len(compacted), 1)
            self.assertEqual(complete.call_args.args[3], [])
            self.assertTrue(any(event["kind"] == "context_compacted" for event in store.events(task_id)))
            self.assertEqual(statuses, ["Compacting context"])
            store.close()

    def test_checkpoint_and_messages_survive_reopening_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = Store(path)
            task_id = store.create(goal="persist", acceptance=[], repo=Path(directory),
                                   worktree=Path(directory), base_commit="base", model="model")
            store.add_message(task_id, "openai", {"role": "user", "content": "old history"})
            store.add_message(task_id, "openai", {"role": "user", "content": "checkpoint"}, checkpoint=True)
            store.add_message(task_id, "openai", {"role": "assistant", "content": "saved response"})
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.messages(task_id), [
                {"role": "user", "content": "checkpoint"},
                {"role": "assistant", "content": "saved response"},
            ])
            reopened.close()

    def test_interrupted_tool_is_closed_without_replaying_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            task_id = store.create(goal="recover", acceptance=[], repo=Path(directory),
                                   worktree=Path(directory), base_commit="base", model="model")
            call = {"type": "function_call", "call_id": "call1", "name": "run_command",
                    "arguments": '{"command":"write a file"}'}
            store.add_message(task_id, "openai", call)
            store.event(task_id, "tool_started", {"call_id": "call1", "name": "run_command"})
            store.event(task_id, "tool_output", {"call_id": "call1", "chunk": "partial output"})

            recovered = runner._recover_interrupted_tools(task_id, "openai", store.messages(task_id), store)

            output = next(item for item in recovered if item.get("type") == "function_call_output")
            self.assertIn("inspect the current repository", output["output"])
            self.assertIn("partial output", output["output"])
            self.assertFalse(any(item.get("role") == "tool" for item in recovered))
            self.assertEqual(sum(event["kind"] == "tool_recovered_after_interruption"
                                 for event in store.events(task_id)), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
