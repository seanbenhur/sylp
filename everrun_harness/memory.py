"""Durable cross-session memory.

Phase 1 (stdlib only): at compaction and task completion an extraction pass
distills durable facts (repository conventions, user preferences, decisions,
failure causes) into the ``memories`` table. Relevant memories are retrieved
with SQLite FTS5 (or a LIKE fallback when FTS5 is unavailable) and injected
into the system prompt at task start and resume.

Phase 2 (optional, off unless configured): a local OpenAI-compatible
embeddings endpoint (``EVERUN_EMBED_URL``/``EVERUN_EMBED_MODEL``, e.g. Ollama)
adds vector similarity, fused with FTS5 through reciprocal rank fusion.
Vectors are indexed with sqlite-vec when that package is installed and fall
back to a Python scan otherwise. A local OpenAI-compatible chat endpoint
(``EVERUN_MEMORY_EXTRACT_URL``/``EVERUN_MEMORY_EXTRACT_MODEL``) can run the
extraction pass instead of the session provider; on any failure extraction
falls back to the session provider, and retrieval falls back to FTS5 only.

Memory is best-effort: extraction is model-generated and can be wrong or
stale, so recalled memories are injected with a verify-before-relying caveat.
Set ``EVERUN_MEMORY=0`` to disable extraction and recall entirely.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Iterable

MEMORY_KINDS = ("convention", "preference", "decision", "failure", "fact")
MEMORY_SCOPES = ("repo", "user")
MAX_MEMORIES_PER_PASS = 8
MAX_MEMORY_CHARS = 500
RECALL_LIMIT = 5
RECALL_MEMORY_CHARS = 300
EXTRACT_CHAR_LIMIT = 30000
HTTP_TIMEOUT = 30

EXTRACTION_SYSTEM = (
    "You extract durable memories from a coding-agent session. "
    "Reply with a JSON array only."
)

CompleteFn = Callable[..., dict[str, Any]]
EmbedFn = Callable[[list[str]], list[list[float]]]


def memory_disabled() -> bool:
    return os.environ.get("EVERUN_MEMORY", "").lower() in {"0", "false", "off", "no"}


def _embed_config() -> tuple[str, str] | None:
    url = os.environ.get("EVERUN_EMBED_URL", "").strip()
    model = os.environ.get("EVERUN_EMBED_MODEL", "").strip()
    return (url.rstrip("/"), model) if url and model else None


def _extractor_config() -> tuple[str, str, str | None] | None:
    url = os.environ.get("EVERUN_MEMORY_EXTRACT_URL", "").strip()
    model = os.environ.get("EVERUN_MEMORY_EXTRACT_MODEL", "").strip()
    key = os.environ.get("EVERUN_MEMORY_EXTRACT_KEY", "").strip() or None
    return (url.rstrip("/"), model, key) if url and model else None


def _post_json(url: str, payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def embed_texts(texts: list[str], *, config: tuple[str, str] | None = None) -> list[list[float]]:
    """Embed texts with an OpenAI-compatible embeddings endpoint (e.g. Ollama)."""
    resolved = config or _embed_config()
    if not resolved:
        raise RuntimeError("Set EVERUN_EMBED_URL and EVERUN_EMBED_MODEL to enable embeddings")
    url, model = resolved
    data = _post_json(f"{url}/embeddings", {"model": model, "input": texts},
                      os.environ.get("EVERUN_EMBED_KEY", "").strip() or None)
    items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
    vectors = [item.get("embedding") for item in items]
    if len(vectors) != len(texts) or any(not isinstance(v, list) for v in vectors):
        raise RuntimeError("Embeddings endpoint returned an unexpected response shape")
    return vectors


def _extraction_prompt(text: str) -> str:
    return f"""Below is an excerpt from a coding-agent session. Extract up to {MAX_MEMORIES_PER_PASS} durable facts worth remembering for future sessions: repository conventions (build/test commands, style rules, layout), user preferences, decisions and their reasons, and root causes of failures. Skip ephemeral task state, file contents, and anything already obvious from the repository itself.

Return a JSON array. Each item: {{"kind": one of {"/".join(MEMORY_KINDS)}, "scope": "repo" (about this repository) or "user" (about the person), "content": one sentence under 400 characters}}. Return [] when nothing is durable. JSON array only, no commentary.

Session excerpt:
{text}"""


def parse_memories(raw: str) -> list[dict[str, str]]:
    """Leniently parse the extractor's reply into validated memory candidates."""
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    parsed = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        scope = str(item.get("scope") or "").strip().lower()
        content = " ".join(str(item.get("content") or "").split())
        if not content or kind not in MEMORY_KINDS or scope not in MEMORY_SCOPES:
            continue
        parsed.append({"kind": kind, "scope": scope, "content": content[:MAX_MEMORY_CHARS]})
        if len(parsed) >= MAX_MEMORIES_PER_PASS:
            break
    return parsed


def extract_memories(store, task: dict[str, Any], provider: str, text: str, *,
                     source: str, complete_fn: CompleteFn | None = None,
                     embed_fn: EmbedFn | None = None) -> list[dict[str, Any]]:
    """Run one extraction pass and save new memories. Never raises."""
    from . import providers  # local import: keeps tests free to patch runner.complete

    task_id = str(task.get("id") or "")
    if memory_disabled() or not text.strip():
        return []
    prompt = _extraction_prompt(text[-EXTRACT_CHAR_LIMIT:])
    raw = None
    extractor = _extractor_config()
    if extractor:
        url, model, key = extractor
        try:
            data = _post_json(
                f"{url}/chat/completions",
                {"model": model, "temperature": 0,
                 "messages": [{"role": "system", "content": EXTRACTION_SYSTEM},
                              {"role": "user", "content": prompt}]},
                key,
            )
            raw = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
        except Exception as exc:
            store.event(task_id, "memory_local_extractor_failed",
                        {"source": source, "error": str(exc)[:500]})
    if raw is None:
        complete = complete_fn or providers.complete
        try:
            result = complete(provider, task.get("model") or "",
                              [{"role": "user", "content": prompt}], [],
                              EXTRACTION_SYSTEM, stream_output=False)
            raw = str(result.get("text") or "")
        except Exception as exc:
            store.event(task_id, "memory_extraction_failed",
                        {"source": source, "error": str(exc)[:500]})
            return []
    candidates = parse_memories(raw)
    saved = []
    for item in candidates:
        repo = task.get("repo") if item["scope"] == "repo" else None
        existing_id = store.find_memory(repo, item["content"])
        if existing_id is not None:
            store.touch_memory(existing_id)
            continue
        embedding = None
        if _embed_config():
            try:
                embedding = (embed_fn or embed_texts)([item["content"]])[0]
            except Exception as exc:
                store.event(task_id, "memory_embedding_failed",
                            {"source": source, "error": str(exc)[:500]})
        memory_id = store.add_memory(repo, item["kind"], item["content"],
                                     source_task_id=task_id or None, embedding=embedding)
        saved.append({"id": memory_id, "kind": item["kind"], "scope": item["scope"],
                      "content": item["content"]})
    store.event(task_id, "memories_extracted",
                {"source": source, "candidates": len(candidates), "saved": len(saved)})
    return saved


def transcript_excerpt(history: Iterable[dict[str, Any]], *, limit: int = EXTRACT_CHAR_LIMIT) -> str:
    """Render provider-shaped history messages as plain text for extraction."""
    chunks: list[str] = []
    for message in history:
        role = str(message.get("role") or message.get("type") or "")
        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text", "input_text"}:
                    texts.append(str(item.get("text") or ""))
        if message.get("type") == "function_call_output":
            texts.append(str(message.get("output") or ""))
        detail = "\n".join(text for text in texts if text).strip()
        if detail:
            chunks.append(f"{role}: {detail}")
    return "\n\n".join(chunks)[-limit:]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a ** 0.5 * norm_b ** 0.5)


def _vector_ranked(store, repo: str | None, query_embedding: list[float], limit: int) -> list[int]:
    """Memory ids ordered by vector similarity, sqlite-vec first, Python scan otherwise."""
    knn = store.vec_knn(query_embedding, repo, limit)
    if knn is not None:
        return knn
    scored = [(row["id"], _cosine(query_embedding, json.loads(row["embedding"])))
              for row in store.memories_with_embeddings(repo)]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [memory_id for memory_id, score in scored[:limit] if score > 0]


def _rrf(*rankings: list[int], k: int = 60) -> list[int]:
    """Reciprocal rank fusion over ordered id lists."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, memory_id in enumerate(ranking):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + position + 1)
    return sorted(scores, key=scores.__getitem__, reverse=True)


def recall(store, repo: str | None, query: str, *, limit: int = RECALL_LIMIT,
           embed_fn: EmbedFn | None = None) -> list[dict[str, Any]]:
    """Hybrid retrieval: FTS5 (or LIKE) fused with vector similarity when configured."""
    if memory_disabled() or not query.strip():
        return []
    fts_hits = store.search_memories(query, repo, limit=limit * 4)
    if not _embed_config():
        return fts_hits[:limit]
    try:
        query_embedding = (embed_fn or embed_texts)([query])[0]
        vector_ids = _vector_ranked(store, repo, query_embedding, limit * 4)
    except Exception:
        vector_ids = []
    if not vector_ids:
        return fts_hits[:limit]
    fused = _rrf([hit["id"] for hit in fts_hits], vector_ids)
    by_id = {hit["id"]: hit for hit in fts_hits}
    missing = [memory_id for memory_id in fused if memory_id not in by_id]
    if missing:
        by_id.update(store.memories_by_ids(missing))
    return [by_id[memory_id] for memory_id in fused[:limit] if memory_id in by_id]


def recall_block(store, repo: str | None, query: str, **kwargs: Any) -> str:
    """Format recalled memories for injection into the system prompt."""
    try:
        memories = recall(store, repo, query, **kwargs)
    except Exception:
        return ""
    if not memories:
        return ""
    lines = ["Memories from past Everrun sessions (model-extracted hints that may be "
             "stale or wrong; verify against the repository before relying on them):"]
    for memory in memories:
        content = memory["content"][:RECALL_MEMORY_CHARS]
        lines.append(f"- [{memory['kind']}] {content}")
    return "\n".join(lines)
