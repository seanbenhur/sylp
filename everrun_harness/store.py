from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


def default_db() -> Path:
    configured_home = os.environ.get("EVERUN_HOME") or os.environ.get("RELAY_HOME")
    if configured_home:
        return Path(configured_home) / "state.sqlite3"
    # Keep using the earlier state location if one exists so a rename doesn't strand task history.
    everrun_home = Path.home() / ".everrun"
    legacy_home = Path.home() / ".relay"
    if not (everrun_home / "state.sqlite3").exists() and (legacy_home / "state.sqlite3").exists():
        return legacy_home / "state.sqlite3"
    return everrun_home / "state.sqlite3"


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or default_db()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.db = sqlite3.connect(self.path, timeout=30)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, goal TEXT NOT NULL, acceptance TEXT NOT NULL,
              repo TEXT NOT NULL, worktree TEXT NOT NULL, base_commit TEXT NOT NULL,
              status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL,
              provider TEXT NOT NULL DEFAULT 'openai', auto_approve INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
              at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
              provider TEXT NOT NULL, payload TEXT NOT NULL, checkpoint INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              repo TEXT,
              kind TEXT NOT NULL DEFAULT 'fact',
              content TEXT NOT NULL,
              source_task_id TEXT,
              embedding TEXT,
              created REAL NOT NULL, updated REAL NOT NULL
            );
        """)
        # FTS5 is compiled into stock CPython SQLite but is technically optional;
        # degrade to LIKE search when it is missing.
        self._fts5 = True
        try:
            self.db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                  content, content='memories', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
                  INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;
            """)
        except sqlite3.OperationalError:
            self._fts5 = False
        if self._fts5:
            # Cover rows inserted before the index existed (e.g. FTS5 was once unavailable).
            memory_count = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            fts_count = self.db.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
            if memory_count != fts_count:
                self.db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        self._vec_module: Any = None  # None = untried, False = unavailable
        self.db.commit()
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "provider" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN provider TEXT NOT NULL DEFAULT 'openai'")
        if "auto_approve" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0")
        message_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "checkpoint" not in message_columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN checkpoint INTEGER NOT NULL DEFAULT 0")
        memory_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(memories)")}
        if "embedding" not in memory_columns:
            self.db.execute("ALTER TABLE memories ADD COLUMN embedding TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def create(self, *, goal: str, acceptance: list[str], repo: Path,
               worktree: Path, base_commit: str, model: str = "", provider: str = "openai",
               auto_approve: bool = False) -> str:
        task_id = uuid.uuid4().hex[:10]
        now = time.time()
        self.db.execute("INSERT INTO tasks(id,goal,acceptance,repo,worktree,base_commit,status,model,provider,auto_approve,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, goal, json.dumps(acceptance), str(repo), str(worktree), base_commit, "running", model, provider, int(auto_approve), now, now))
        self.db.commit()
        self.event(task_id, "created", {"goal": goal, "acceptance": acceptance, "base_commit": base_commit})
        return task_id

    def event(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        now = time.time()
        self.db.execute("INSERT INTO events(task_id,at,kind,payload) VALUES(?,?,?,?)", (task_id, now, kind, json.dumps(payload)))
        self.db.execute("UPDATE tasks SET updated=? WHERE id=?", (now, task_id))
        self.db.commit()

    def update(self, task_id: str, **values: Any) -> None:
        values["updated"] = time.time()
        fields = ",".join(f"{key}=?" for key in values)
        self.db.execute(f"UPDATE tasks SET {fields} WHERE id=?", (*values.values(), task_id))
        self.db.commit()

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["acceptance"] = json.loads(result["acceptance"])
        return result

    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT id,goal,repo,provider,model,status,updated FROM tasks ORDER BY updated DESC")]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT at,kind,payload FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

    def messages(self, task_id: str) -> list[dict[str, Any]]:
        checkpoint = self.db.execute("SELECT MAX(seq) FROM messages WHERE task_id=? AND checkpoint=1", (task_id,)).fetchone()[0]
        if checkpoint:
            rows = self.db.execute("SELECT payload FROM messages WHERE task_id=? AND seq>=? ORDER BY seq", (task_id, checkpoint))
        else:
            rows = self.db.execute("SELECT payload FROM messages WHERE task_id=? ORDER BY seq", (task_id,))
        return [json.loads(r["payload"]) for r in rows]

    def add_message(self, task_id: str, provider: str, payload: dict[str, Any], *, checkpoint: bool = False) -> None:
        self.db.execute("INSERT INTO messages(task_id,provider,payload,checkpoint,created) VALUES(?,?,?,?,?)",
                        (task_id, provider, json.dumps(payload), int(checkpoint), time.time()))
        self.db.commit()

    def append_anthropic_tool_result(self, task_id: str, tool_use_id: str, result: str) -> None:
        row = self.db.execute("SELECT seq,payload FROM messages WHERE task_id=? ORDER BY seq DESC LIMIT 1", (task_id,)).fetchone()
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": result}
        if row:
            payload = json.loads(row["payload"])
            content = payload.get("content")
            if payload.get("role") == "user" and isinstance(content, list) and all(item.get("type") == "tool_result" for item in content):
                content.append(block)
                self.db.execute("UPDATE messages SET payload=? WHERE seq=?", (json.dumps(payload), row["seq"]))
                self.db.commit()
                return
        self.add_message(task_id, "anthropic", {"role": "user", "content": [block]})

    def clear_messages(self, task_id: str) -> None:
        self.db.execute("DELETE FROM messages WHERE task_id=?", (task_id,))
        self.db.commit()

    # --- Durable memories (see memory.py) ---

    @staticmethod
    def _normalized(content: str) -> str:
        return " ".join(content.lower().split())

    def add_memory(self, repo: str | None, kind: str, content: str, *,
                   source_task_id: str | None = None,
                   embedding: list[float] | None = None) -> int:
        now = time.time()
        cursor = self.db.execute(
            "INSERT INTO memories(repo,kind,content,source_task_id,embedding,created,updated) "
            "VALUES(?,?,?,?,?,?,?)",
            (repo, kind, content, source_task_id,
             json.dumps(embedding) if embedding else None, now, now))
        memory_id = int(cursor.lastrowid)
        if embedding:
            self._vec_upsert(memory_id, embedding)
        self.db.commit()
        return memory_id

    def find_memory(self, repo: str | None, content: str) -> int | None:
        """Id of an existing memory with the same normalized content in this scope."""
        wanted = self._normalized(content)
        for row in self.db.execute("SELECT id,content FROM memories WHERE repo IS ?", (repo,)):
            if self._normalized(row["content"]) == wanted:
                return int(row["id"])
        return None

    def touch_memory(self, memory_id: int) -> None:
        self.db.execute("UPDATE memories SET updated=? WHERE id=?", (time.time(), memory_id))
        self.db.commit()

    def delete_memory(self, memory_id: int) -> bool:
        cursor = self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if self._sqlite_vec() is not None:
            try:
                self.db.execute("DELETE FROM memories_vec WHERE rowid=?", (memory_id,))
            except sqlite3.Error:
                pass
        self.db.commit()
        return cursor.rowcount > 0

    def list_memories(self, repo: str | None = None, *, include_all: bool = False) -> list[dict[str, Any]]:
        if include_all:
            rows = self.db.execute("SELECT * FROM memories ORDER BY updated DESC")
        elif repo:
            rows = self.db.execute(
                "SELECT * FROM memories WHERE repo=? OR repo IS NULL ORDER BY updated DESC", (repo,))
        else:
            rows = self.db.execute("SELECT * FROM memories WHERE repo IS NULL ORDER BY updated DESC")
        return [dict(r) for r in rows]

    def memories_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        return {int(r["id"]): dict(r) for r in
                self.db.execute(f"SELECT * FROM memories WHERE id IN ({marks})", ids)}

    def memories_with_embeddings(self, repo: str | None) -> list[dict[str, Any]]:
        scope = "AND (repo = ? OR repo IS NULL)" if repo else "AND repo IS NULL"
        params = (repo,) if repo else ()
        return [dict(r) for r in self.db.execute(
            f"SELECT id,embedding FROM memories WHERE embedding IS NOT NULL {scope}", params)]

    def search_memories(self, query: str, repo: str | None, *, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 keyword search scoped to one repo plus user-scope memories; LIKE fallback."""
        tokens = re.findall(r"\w+", query.lower())[:8]
        if not tokens:
            return []
        scope_sql = "(m.repo = ? OR m.repo IS NULL)" if repo else "m.repo IS NULL"
        scope_params: tuple[Any, ...] = (repo,) if repo else ()
        if self._fts5:
            match = " OR ".join('"' + token.replace('"', "") + '"' for token in tokens)
            try:
                return [dict(r) for r in self.db.execute(
                    "SELECT m.id, m.repo, m.kind, m.content, m.source_task_id, m.updated "
                    "FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    f"WHERE memories_fts MATCH ? AND {scope_sql} "
                    "ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, *scope_params, limit))]
            except sqlite3.OperationalError:
                pass  # unusable FTS index or query; fall back to LIKE
        where = " OR ".join("m.content LIKE ?" for _ in tokens)
        return [dict(r) for r in self.db.execute(
            "SELECT m.id, m.repo, m.kind, m.content, m.source_task_id, m.updated "
            f"FROM memories m WHERE {scope_sql} AND ({where}) ORDER BY m.updated DESC LIMIT ?",
            (*scope_params, *(f"%{token}%" for token in tokens), limit))]

    # --- sqlite-vec index (optional; memory.py falls back to a Python scan) ---

    def _sqlite_vec(self):
        if self._vec_module is None:
            try:
                import sqlite_vec
                sqlite_vec.load(self.db)
                self._vec_module = sqlite_vec
            except Exception:
                self._vec_module = False
        return self._vec_module or None

    def _ensure_vec_index(self, dims: int) -> bool:
        """Create (or rebuild after an embedding-model change) the vec0 index."""
        module = self._sqlite_vec()
        if module is None:
            return False
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS memories_vec_meta(dims INTEGER NOT NULL)")
            row = self.db.execute("SELECT dims FROM memories_vec_meta").fetchone()
            if row and int(row["dims"]) != dims:
                self.db.execute("DROP TABLE IF EXISTS memories_vec")
                self.db.execute("DELETE FROM memories_vec_meta")
                row = None
            if row is None:
                self.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec "
                                f"USING vec0(embedding float[{dims}])")
                self.db.execute("INSERT INTO memories_vec_meta(dims) VALUES(?)", (dims,))
                for entry in self.db.execute(
                        "SELECT id,embedding FROM memories WHERE embedding IS NOT NULL"):
                    vector = json.loads(entry["embedding"])
                    if len(vector) == dims:
                        self.db.execute("INSERT INTO memories_vec(rowid, embedding) VALUES(?,?)",
                                        (entry["id"], module.serialize_float32(vector)))
            self.db.commit()
            return True
        except sqlite3.Error:
            return False

    def _vec_upsert(self, memory_id: int, embedding: list[float]) -> None:
        module = self._sqlite_vec()
        if module is None or not self._ensure_vec_index(len(embedding)):
            return
        try:
            self.db.execute("DELETE FROM memories_vec WHERE rowid=?", (memory_id,))
            self.db.execute("INSERT INTO memories_vec(rowid, embedding) VALUES(?,?)",
                            (memory_id, module.serialize_float32(embedding)))
        except sqlite3.Error:
            pass

    def vec_knn(self, query_embedding: list[float], repo: str | None, limit: int) -> list[int] | None:
        """Memory ids by vector similarity via sqlite-vec, or None when unavailable."""
        module = self._sqlite_vec()
        if module is None or not self._ensure_vec_index(len(query_embedding)):
            return None
        scope_sql = "AND (m.repo = ? OR m.repo IS NULL)" if repo else "AND m.repo IS NULL"
        params: list[Any] = [module.serialize_float32(query_embedding), limit * 4]
        if repo:
            params.append(repo)
        try:
            rows = self.db.execute(
                "SELECT v.rowid AS id FROM memories_vec v JOIN memories m ON m.id = v.rowid "
                f"WHERE v.embedding MATCH ? AND k = ? {scope_sql} ORDER BY v.distance",
                params).fetchall()
            return [int(r["id"]) for r in rows[:limit]]
        except sqlite3.Error:
            return None
