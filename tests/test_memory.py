from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from everrun_harness import memory, runner
from everrun_harness.git import prepare_worktree
from everrun_harness.store import Store

MEMORY_ENV = ["EVERUN_MEMORY", "EVERUN_EMBED_URL", "EVERUN_EMBED_MODEL", "EVERUN_EMBED_KEY",
              "EVERUN_MEMORY_EXTRACT_URL", "EVERUN_MEMORY_EXTRACT_MODEL", "EVERUN_MEMORY_EXTRACT_KEY"]


def clean_env(**overrides):
    values = {key: "" for key in MEMORY_ENV}
    values.update(overrides)
    return patch.dict(os.environ, values)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._directory.name) / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self._directory.cleanup()

    def test_add_search_and_scope(self):
        self.store.add_memory("/repo/a", "convention", "Tests run with unittest discover.")
        self.store.add_memory(None, "preference", "The user prefers stdlib-only dependencies.")
        self.store.add_memory("/repo/b", "fact", "Repo B deploys with Ansible.")

        hits = self.store.search_memories("unittest", "/repo/a")
        self.assertEqual([hit["kind"] for hit in hits], ["convention"])

        hits = self.store.search_memories("stdlib", "/repo/a")
        self.assertEqual(len(hits), 1)  # user-scope memory is visible from any repo

        hits = self.store.search_memories("Ansible", "/repo/a")
        self.assertEqual(hits, [])  # repo B memories stay out of repo A

    def test_like_fallback_when_fts5_unavailable(self):
        self.store.add_memory("/repo/a", "convention", "Build with make wheel.")
        self.store._fts5 = False
        hits = self.store.search_memories("make", "/repo/a")
        self.assertEqual(len(hits), 1)
        self.assertIn("make wheel", hits[0]["content"])

    def test_dedupe_by_normalized_content_and_touch(self):
        first = self.store.add_memory(None, "preference", "The user likes  terse   replies.")
        found = self.store.find_memory(None, "the user likes terse replies.")
        self.assertEqual(found, first)
        self.store.touch_memory(first)
        self.assertEqual(len(self.store.list_memories()), 1)

    def test_delete_memory(self):
        memory_id = self.store.add_memory("/repo/a", "fact", "Temporary fact.")
        self.assertTrue(self.store.delete_memory(memory_id))
        self.assertFalse(self.store.delete_memory(memory_id))
        self.assertEqual(self.store.search_memories("Temporary", "/repo/a"), [])

    def test_search_matches_multiple_memories(self):
        self.store.add_memory("/repo/a", "fact", "alpha token fact")
        self.store.add_memory("/repo/a", "fact", "beta token fact")
        hits = self.store.search_memories("token", "/repo/a")
        self.assertEqual(len(hits), 2)


class ParseTests(unittest.TestCase):
    def test_parses_json_inside_prose(self):
        raw = ('Here you go:\n[{"kind": "convention", "scope": "repo", "content": "Use ruff."}]'
               "\nHope that helps")
        self.assertEqual(memory.parse_memories(raw),
                         [{"kind": "convention", "scope": "repo", "content": "Use ruff."}])

    def test_drops_invalid_items_and_caps_count(self):
        items = [{"kind": "fact", "scope": "repo", "content": f"fact {index}"}
                 for index in range(memory.MAX_MEMORIES_PER_PASS + 3)]
        items.append({"kind": "nonsense", "scope": "repo", "content": "bad kind"})
        items.append({"kind": "fact", "scope": "repo"})
        parsed = memory.parse_memories(json.dumps(items))
        self.assertEqual(len(parsed), memory.MAX_MEMORIES_PER_PASS)
        self.assertTrue(all(item["kind"] == "fact" for item in parsed))

    def test_empty_and_garbage_replies(self):
        self.assertEqual(memory.parse_memories(""), [])
        self.assertEqual(memory.parse_memories("no json here"), [])
        self.assertEqual(memory.parse_memories("[{broken"), [])
        self.assertEqual(memory.parse_memories('{"kind": "fact"}'), [])

    def test_long_content_is_capped(self):
        raw = json.dumps([{"kind": "fact", "scope": "user", "content": "x" * 1000}])
        self.assertEqual(len(memory.parse_memories(raw)[0]["content"]), memory.MAX_MEMORY_CHARS)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._directory.name) / "state.sqlite3")
        self.task = {"id": "task1", "model": "fake", "repo": "/repo/a"}

    def tearDown(self):
        self.store.close()
        self._directory.cleanup()

    def test_extracts_and_saves_memories_with_provider(self):
        reply = json.dumps([
            {"kind": "convention", "scope": "repo", "content": "Tests run with unittest."},
            {"kind": "preference", "scope": "user", "content": "The user wants stdlib-only code."},
        ])
        with clean_env():
            saved = memory.extract_memories(
                self.store, self.task, "openai", "user: how do tests run?",
                source="compaction", complete_fn=lambda *a, **k: {"text": reply})
        self.assertEqual(len(saved), 2)
        repo_rows = self.store.list_memories("/repo/a")
        self.assertEqual(len(repo_rows), 2)  # repo + user scope
        self.assertEqual(sum(row["repo"] is None for row in repo_rows), 1)
        event = next(e for e in self.store.events("task1") if e["kind"] == "memories_extracted")
        self.assertEqual(json.loads(event["payload"]), {"source": "compaction", "candidates": 2, "saved": 2})

    def test_second_pass_dedupes_instead_of_duplicating(self):
        reply = json.dumps([{"kind": "fact", "scope": "repo", "content": "The CLI entry point is cli.py."}])
        with clean_env():
            memory.extract_memories(self.store, self.task, "openai", "text", source="compaction",
                                    complete_fn=lambda *a, **k: {"text": reply})
            saved = memory.extract_memories(self.store, self.task, "openai", "text", source="completion",
                                            complete_fn=lambda *a, **k: {"text": reply})
        self.assertEqual(saved, [])
        self.assertEqual(len(self.store.list_memories("/repo/a")), 1)

    def test_failing_local_extractor_falls_back_to_provider(self):
        reply = json.dumps([{"kind": "fact", "scope": "repo", "content": "Fallback works."}])
        env = clean_env(EVERUN_MEMORY_EXTRACT_URL="http://localhost:9/v1",
                        EVERUN_MEMORY_EXTRACT_MODEL="tiny")
        with env, patch.object(memory, "_post_json", side_effect=OSError("connection refused")):
            saved = memory.extract_memories(
                self.store, self.task, "openai", "text", source="compaction",
                complete_fn=lambda *a, **k: {"text": reply})
        self.assertEqual(len(saved), 1)
        self.assertTrue(any(e["kind"] == "memory_local_extractor_failed"
                            for e in self.store.events("task1")))

    def test_provider_failure_is_an_event_not_an_exception(self):
        def failing(*args, **kwargs):
            raise RuntimeError("provider down")

        with clean_env():
            saved = memory.extract_memories(self.store, self.task, "openai", "text",
                                            source="completion", complete_fn=failing)
        self.assertEqual(saved, [])
        self.assertTrue(any(e["kind"] == "memory_extraction_failed"
                            for e in self.store.events("task1")))

    def test_disabled_memory_skips_extraction(self):
        with clean_env(EVERUN_MEMORY="0"):
            saved = memory.extract_memories(
                self.store, self.task, "openai", "text", source="compaction",
                complete_fn=lambda *a, **k: self.fail("extractor ran while disabled"))
        self.assertEqual(saved, [])

    def test_extraction_stores_embeddings_when_configured(self):
        reply = json.dumps([{"kind": "fact", "scope": "repo", "content": "Embeddings attach."}])
        env = clean_env(EVERUN_EMBED_URL="http://localhost:9", EVERUN_EMBED_MODEL="embed-model")
        with env:
            memory.extract_memories(self.store, self.task, "openai", "text", source="compaction",
                                    complete_fn=lambda *a, **k: {"text": reply},
                                    embed_fn=lambda texts: [[1.0, 0.0] for _ in texts])
        rows = self.store.memories_with_embeddings("/repo/a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["embedding"]), [1.0, 0.0])


class RecallTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._directory.name) / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self._directory.cleanup()

    def test_fts_only_recall_and_block_format(self):
        self.store.add_memory("/repo/a", "convention", "Tests run with unittest discover -s tests.")
        self.store.add_memory("/repo/a", "fact", "The sandbox uses bubblewrap.")
        with clean_env():
            hits = memory.recall(self.store, "/repo/a", "how do I run the unittest suite?")
            block = memory.recall_block(self.store, "/repo/a", "how do I run the unittest suite?")
        self.assertEqual(hits[0]["kind"], "convention")
        self.assertIn("Memories from past Everrun sessions", block)
        self.assertIn("verify against the repository", block)
        self.assertIn("[convention] Tests run with unittest", block)

    def test_recall_block_is_empty_without_matches_or_when_disabled(self):
        self.store.add_memory("/repo/a", "fact", "Something stored.")
        with clean_env():
            self.assertEqual(memory.recall_block(self.store, "/repo/a", "zzzzqqqq"), "")
        with clean_env(EVERUN_MEMORY="0"):
            self.assertEqual(memory.recall_block(self.store, "/repo/a", "Something"), "")

    def test_hybrid_recall_surfaces_vector_only_matches(self):
        # FTS can match the first memory; only vectors can match the second.
        self.store.add_memory("/repo/a", "convention", "Run tests with unittest.",
                              embedding=[1.0, 0.0, 0.0])
        self.store.add_memory("/repo/a", "fact", "qwen3-4b handles extraction reliably.",
                              embedding=[0.0, 1.0, 0.0])
        embeddings = {"run the unittest suite": [1.0, 0.0, 0.0],
                      "which local model pulls facts out of transcripts": [0.0, 0.99, 0.01]}

        def embed_fn(texts):
            return [embeddings[text] for text in texts]

        env = clean_env(EVERUN_EMBED_URL="http://localhost:9", EVERUN_EMBED_MODEL="embed-model")
        with env:
            fts_hits = memory.recall(self.store, "/repo/a", "run the unittest suite")
            hybrid_hits = memory.recall(self.store, "/repo/a",
                                        "which local model pulls facts out of transcripts",
                                        embed_fn=embed_fn)
        self.assertTrue(any("unittest" in hit["content"] for hit in fts_hits))
        self.assertTrue(any("qwen3-4b" in hit["content"] for hit in hybrid_hits))
        self.assertNotIn("qwen3-4b", " ".join(hit["content"] for hit in fts_hits))

    def test_embedding_failure_falls_back_to_fts(self):
        self.store.add_memory("/repo/a", "convention", "Run tests with unittest.")

        def broken_embed(texts):
            raise OSError("embedding server down")

        env = clean_env(EVERUN_EMBED_URL="http://localhost:9", EVERUN_EMBED_MODEL="embed-model")
        with env:
            hits = memory.recall(self.store, "/repo/a", "unittest", embed_fn=broken_embed)
        self.assertEqual(len(hits), 1)


class RunnerIntegrationTests(unittest.TestCase):
    def test_compaction_runs_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            task_id = store.create(goal="checkpoint", acceptance=[], repo=Path(directory),
                                   worktree=Path(directory), base_commit="base", model="model")
            history = [{"role": "user", "content": "remember the test command"}]
            reply = json.dumps([{"kind": "convention", "scope": "repo",
                                 "content": "Tests use unittest discover."}])
            responses = iter([{"text": "checkpoint text"}, {"text": reply}])
            with clean_env(), \
                    patch.object(runner, "complete", side_effect=lambda *a, **k: next(responses)), \
                    contextlib.redirect_stdout(io.StringIO()):
                runner._compact_history(task_id, {"id": task_id, "model": "model",
                                                  "repo": str(directory)},
                                        "openai", history, store, force=True)
            rows = store.list_memories(str(directory))
            self.assertEqual([row["content"] for row in rows], ["Tests use unittest discover."])
            store.close()

    def test_system_prompt_carries_memory_block(self):
        task = {"goal": "g", "acceptance": []}
        prompt = runner._system_prompt(task, memory_block="Memories from past Everrun sessions:\n- [fact] x")
        self.assertIn("Memories from past Everrun sessions", prompt)
        self.assertLess(prompt.index("- [fact] x"), prompt.index("Task goal:"))
        self.assertNotIn("Memories from past Everrun sessions", runner._system_prompt(task))

    def test_recalled_memories_reach_the_model_prompt_and_completion_extracts(self):
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
            env = clean_env(EVERUN_HOME=str(state))
            with env:
                worktree, base = prepare_worktree(root, "memoryint")
                store = Store()
                task_id = store.create(goal="Report the unittest convention.", acceptance=[],
                                       repo=root, worktree=worktree, base_commit=base,
                                       model="fake-model", provider="openai", auto_approve=True)
                store.add_memory(str(root), "convention", "Tests run with unittest discover -s tests.")
                replies = iter([
                    {"text": "The convention is unittest.", "tool_calls": [],
                     "history_items": [{"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text",
                                                     "text": "The convention is unittest."}]}],
                     "response_id": "r1"},
                    {"text": json.dumps([{"kind": "decision", "scope": "repo",
                                          "content": "State lives in one SQLite database."}])},
                ])
                systems = []

                def fake_complete(*args, **kwargs):
                    if args[4] == memory.EXTRACTION_SYSTEM:
                        return next(replies)
                    systems.append(args[4])
                    return next(replies)

                with patch.object(runner, "complete", side_effect=fake_complete), \
                        contextlib.redirect_stdout(io.StringIO()):
                    runner.run_task(task_id)

                self.assertEqual(len(systems), 1)
                self.assertIn("[convention] Tests run with unittest discover -s tests.", systems[0])
                contents = [row["content"] for row in store.list_memories(str(root))]
                self.assertIn("State lives in one SQLite database.", contents)  # from completion extraction
                store.close()


if __name__ == "__main__":
    unittest.main()
