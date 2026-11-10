from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .extensions import ExtensionSet
from .git import common_dir, diff_summary, refresh_clean_worktree
from .providers import EmptyModelResponse, complete, list_models, save_default_model
from .sandbox import SandboxPolicy, policy_from_env
from .store import Store
from .tools import TOOL_SCHEMAS, execute_tool

MAX_TURNS_PER_INVOCATION = 200
HISTORY_CHAR_LIMIT = 50000
StatusCallback = Callable[[str], None] | None


def _set_status(callback: StatusCallback, value: str) -> None:
    if callback:
        callback(value)


def task_home(task_id: str) -> Path:
    configured_home = os.environ.get("EVERUN_HOME") or os.environ.get("RELAY_HOME")
    if configured_home:
        return Path(configured_home) / "tasks" / task_id
    everrun_home = Path.home() / ".everrun"
    legacy_home = Path.home() / ".relay"
    if not (everrun_home / "state.sqlite3").exists() and (legacy_home / "state.sqlite3").exists():
        return legacy_home / "tasks" / task_id
    return everrun_home / "tasks" / task_id


@contextlib.contextmanager
def _task_lock(task_id: str):
    path = task_home(task_id) / "session.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    handle = path.open("a+")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"Session {task_id} is already open in another Everrun process") from exc
            yield
        else:
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Session {task_id} is already open in another Everrun process") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        if os.name == "posix":
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _system_prompt(task: dict[str, Any], *, interactive: bool = False, extra_tools: bool = False) -> str:
    criteria = "\n".join(f"- {item}" for item in task["acceptance"]) or "- No explicit acceptance criteria supplied"
    extensions_line = ("Tools whose names start with mcp__ come from configured MCP servers; "
                       "other extra tools come from local Everrun plugins.\n\n") if extra_tools else ""
    if interactive:
        ending = "This is an ongoing interactive coding session. Respond naturally to the user's latest message. Continue using tools when useful. Do not emit EVERUN_STATUS markers; after your response, Everrun will wait for the user's next message."
    else:
        ending = "If a consequential ambiguity, destructive action, credential, or external side effect blocks you, stop and ask the user. End your response with exactly `EVERUN_STATUS: WAITING_FOR_INPUT` followed by the question. When the work is complete, end with exactly `EVERUN_STATUS: COMPLETE`, summarize changes, map each acceptance criterion to evidence or mark it unverified, and list the exact checks and results."
    return f"""You are Everrun, a coding agent running inside a persistent local harness. You own the task loop and use the provided tools to inspect and change the repository. The repository is at the current working directory.

Work incrementally. Inspect before editing. Use apply_patch for focused changes, and git_status/git_diff to review the worktree. Keep changes focused and reviewable. Run relevant checks with run_command; it will ask the user for approval unless auto-approval was enabled. Never claim checks passed unless you observed their output.

{extensions_line}{ending}

Task goal:
{task['goal']}

Acceptance criteria:
{criteria}
"""


def _write_report(task: dict[str, Any], task_id: str, summary: str, transcript: str, store: Store) -> Path:
    root = Path(task["worktree"])
    criteria = "\n".join(f"- {item}" for item in task["acceptance"]) or "- No explicit acceptance criteria supplied"
    diff = diff_summary(root, task["base_commit"])
    home = task_home(task_id)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    path = home / "report.md"
    totals = {"input_tokens": 0, "output_tokens": 0}
    usage_seen = False
    for event in store.events(task_id):
        if event["kind"] not in {"model_turn", "empty_model_response"}:
            continue
        try:
            usage = json.loads(event["payload"]).get("usage") or {}
        except (json.JSONDecodeError, TypeError):
            continue
        for target, keys in (("input_tokens", ("input_tokens", "prompt_tokens")),
                             ("output_tokens", ("output_tokens", "completion_tokens"))):
            for key in keys:
                if isinstance(usage.get(key), (int, float)):
                    totals[target] += int(usage[key])
                    usage_seen = True
                    break
    usage_report = (f"## Provider-reported token usage\n\nInput: {totals['input_tokens']:,}; "
                    f"output: {totals['output_tokens']:,}. Billed cost is not inferred.\n\n") if usage_seen else ""
    path.write_text(
        f"# Task {task_id}\n\n## Goal\n{task['goal']}\n\n## Acceptance criteria\n{criteria}\n\n"
        f"{usage_report}## Agent report\n{summary}\n\n## Transcript excerpt\n{transcript[-12000:]}\n\n"
        f"## Diff summary\n```text\n{diff}\n```\n\n"
        "Agent-reported verification is not independently validated by Everrun. Review the diff and rerun checks before merging.\n",
        encoding="utf-8",
    )
    return path


def _tool_outputs(task_id: str, task: dict[str, Any], calls: list[dict[str, Any]], provider: str, store: Store,
                  on_status: StatusCallback = None, extensions: ExtensionSet | None = None,
                  sandbox: SandboxPolicy | None = None) -> list[dict[str, Any]]:
    outputs = []
    for index, call in enumerate(calls):
        name = str(call.get("name") or "")
        _set_status(on_status, "Approval needed" if name == "run_command" and not task["auto_approve"]
                    else f"Running {name}")
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {"_invalid_arguments": arguments}
        store.event(task_id, "tool_started", {"name": name, "arguments": arguments, "call_id": call.get("id")})
        print(f"\n[tool {name} running]", flush=True)
        output_streamed = False
        output_buffer = ""
        output_checkpointed = 0
        last_output_checkpoint = time.monotonic()
        call_id = call.get("id")

        def flush_output() -> None:
            nonlocal output_buffer, output_checkpointed, last_output_checkpoint
            if output_buffer:
                store.event(task_id, "tool_output", {"call_id": call_id, "chunk": output_buffer})
                output_checkpointed += len(output_buffer)
                output_buffer = ""
                last_output_checkpoint = time.monotonic()

        def on_output(chunk: str) -> None:
            nonlocal output_streamed, output_buffer
            output_streamed = True
            remaining = 20000 - output_checkpointed - len(output_buffer)
            if remaining > 0:
                output_buffer += chunk[:remaining]
            if len(output_buffer) >= 2048 or (output_buffer and time.monotonic() - last_output_checkpoint >= 1):
                flush_output()
            print(chunk, end="", flush=True)

        try:
            result = execute_tool(name, arguments, Path(task["worktree"]),
                                  auto_approve=bool(task["auto_approve"]), on_output=on_output,
                                  on_checkpoint=flush_output, extensions=extensions, sandbox=sandbox)
        except KeyboardInterrupt:
            flush_output()
            result = "Tool interrupted by the user. Inspect the worktree before retrying this action."
            _save_tool_output(task_id, provider, call, result, store)
            for pending in calls[index + 1:]:
                _save_tool_output(task_id, provider, pending, "Skipped because the user interrupted tool execution.", store)
            raise
        except Exception as exc:
            flush_output()
            result = f"Tool error ({type(exc).__name__}): {exc}"
        else:
            flush_output()
        store.event(task_id, "tool_finished", {"name": name, "arguments": arguments,
                                                "call_id": call.get("id"), "result": result[:4000]})
        if output_streamed:
            summary = result.partition("\n")[0]
            print(f"\n[tool {name} complete] {summary}", flush=True)
        else:
            print(f"\n[tool {name}]\n{result[:4000]}", flush=True)
        _save_tool_output(task_id, provider, call, result, store)
        outputs.append({"id": call.get("id"), "name": name, "result": result})
    return outputs


def _save_assistant(task_id: str, provider: str, result: dict[str, Any], store: Store) -> None:
    if provider == "openai" and result.get("history_items") is not None:
        for item in result["history_items"]:
            store.add_message(task_id, provider, item)
        return
    assistant = result.get("assistant_message")
    if assistant is not None:
        store.add_message(task_id, provider, assistant)


def _save_tool_output(task_id: str, provider: str, call: dict[str, Any], result: str, store: Store) -> None:
    if provider == "openai":
        payload = {"type": "function_call_output", "call_id": call.get("id"), "output": result}
    elif provider == "anthropic":
        store.append_anthropic_tool_result(task_id, str(call.get("id") or ""), result)
        return
    else:
        payload = {"role": "tool", "tool_call_id": call.get("id"), "content": result}
    store.add_message(task_id, provider, payload)


def _recover_interrupted_tools(task_id: str, provider: str, history: list[dict[str, Any]], store: Store) -> list[dict[str, Any]]:
    """Close unfinished tool calls after a process crash without replaying side effects."""
    completed: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    partial_output: dict[str, list[str]] = {}
    finished_output: dict[str, str] = {}
    for event in store.events(task_id):
        try:
            payload = json.loads(event["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        call_id = payload.get("call_id")
        if call_id and event["kind"] == "tool_output":
            partial_output.setdefault(str(call_id), []).append(str(payload.get("chunk") or ""))
        elif call_id and event["kind"] == "tool_finished":
            finished_output[str(call_id)] = str(payload.get("result") or "")
    if provider == "openai":
        for item in history:
            if item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id")
                if call_id:
                    pending[str(call_id)] = {"id": str(call_id), "name": item.get("name"),
                                             "arguments": _decode_arguments(item.get("arguments"))}
            elif item.get("type") == "function_call_output" and item.get("call_id"):
                completed.add(str(item["call_id"]))
    elif provider == "anthropic":
        for message in history:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use" and item.get("id"):
                    pending[str(item["id"])] = {"id": str(item["id"]), "name": item.get("name"),
                                                 "arguments": item.get("input", {})}
                elif item.get("type") == "tool_result" and item.get("tool_use_id"):
                    completed.add(str(item["tool_use_id"]))
    else:
        for message in history:
            for call in message.get("tool_calls", []) if isinstance(message.get("tool_calls"), list) else []:
                call_id = call.get("id")
                function = call.get("function") or {}
                if call_id:
                    pending[str(call_id)] = {"id": str(call_id), "name": function.get("name"),
                                             "arguments": _decode_arguments(function.get("arguments"))}
            if message.get("role") == "tool" and message.get("tool_call_id"):
                completed.add(str(message["tool_call_id"]))
    unfinished = [call for call_id, call in pending.items() if call_id not in completed]
    for call in unfinished:
        recorded = finished_output.get(str(call["id"]))
        if recorded:
            explanation = (
                "Everrun recorded this tool as finished before the process stopped. Inspect the current state "
                "before repeating it. Recorded result:\n" + recorded
            )
        else:
            explanation = (
                "Everrun stopped while this tool call was in progress. Its effects may be partial or complete. "
                "Do not repeat the action yet; inspect the current repository and relevant system state first."
            )
            captured = "".join(partial_output.get(str(call["id"]), []))
            if captured:
                explanation += "\nPartial output captured before the interruption:\n" + captured[-12000:]
        _save_tool_output(task_id, provider, call, explanation, store)
        store.event(task_id, "tool_recovered_after_interruption", {"name": call.get("name"), "call_id": call["id"]})
    return store.messages(task_id) if unfinished else history


def _decode_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"_invalid_arguments": str(value)}
    return parsed if isinstance(parsed, dict) else {"_invalid_arguments": parsed}


def _session_input(prompt: str = "You> ") -> str:
    while True:
        try:
            value = input(prompt).strip()
        except EOFError:
            return "/exit"
        if value:
            return value
        print("Enter a message or use /exit to pause the session.")


def _session_is_waiting_for_user(history: list[dict[str, Any]]) -> bool:
    if not history:
        return False
    last = history[-1]
    role = last.get("role")
    if role == "user":
        return False
    if role == "assistant":
        content = last.get("content")
        if isinstance(last.get("tool_calls"), list) and last["tool_calls"]:
            return False
        if isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == "tool_use" for item in content):
            return False
        return True
    return last.get("type") == "message" and role == "assistant"


def _show_session_history(history: list[dict[str, Any]]) -> None:
    visible: list[tuple[str, str]] = []
    for message in history:
        role = str(message.get("role") or "")
        content = message.get("content")
        chunks: list[str] = []
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind in {"text", "output_text", "input_text"}:
                    chunks.append(str(item.get("text") or ""))
                elif kind == "tool_use":
                    visible.append(("Everrun", f"Using {item.get('name', 'tool')}"))
        elif message.get("type") == "message" and role == "assistant":
            chunks.extend(str(item.get("text") or "") for item in message.get("content", [])
                          if isinstance(item, dict) and item.get("type") in {"output_text", "text"})
        elif message.get("type") == "function_call":
            visible.append(("Everrun", f"Using {message.get('name', 'tool')}"))
        detail = "\n".join(chunk for chunk in chunks if chunk).strip()
        if detail and role in {"user", "assistant"}:
            visible.append(("You" if role == "user" else "Everrun", detail))
    print("Recent conversation:")
    for role, detail in visible[-30:]:
        print(f"{role}> {detail[:1200]}")
    print()


def _read_session_message(task: dict[str, Any], store: Store, provider: str) -> str:
    while True:
        value = _session_input()
        if value in {"/exit", "/quit", "/done"}:
            return value
        if value == "/help":
            print("/help  show commands\n/model [id]  show or switch models\n/status  show session details\n/files  list repository files\n/diff  inspect task changes\n/switch [id]  list paused sessions or switch to one\n/compact  save a conversation checkpoint\n/exit  pause and save\n/done  finish and create a review report")
            continue
        if value == "/status":
            print(f"Session {task['id']} · {task['status']} · {provider}/{task['model']}\nWorktree: {task['worktree']}")
            continue
        if value == "/files":
            print(execute_tool("list_files", {"path": "."}, Path(task["worktree"])))
            continue
        if value == "/diff":
            root = Path(task["worktree"])
            print(execute_tool("git_status", {}, root))
            print(execute_tool("git_diff", {}, root))
            continue
        if value == "/model" or value.startswith("/model "):
            model = value.partition(" ")[2].strip()
            if not model:
                for item in list_models(provider):
                    print(f"  {item['id']}  ({item['name']})")
                model = _session_input("Model ID> ")
            if model in {"/exit", "/quit"}:
                return "/exit"
            if model:
                task["model"] = model
                store.update(task["id"], model=model)
                save_default_model(provider, model)
                print(f"Using {provider}/{model}.")
            continue
        if value == "/switch" or value.startswith("/switch "):
            target_id = value.partition(" ")[2].strip()
            repo_key = str(Path(task["repo"]).resolve())
            if not target_id:
                switchable = [item for item in store.list()
                              if item["id"] != task["id"]
                              and item["status"] in {"paused", "waiting_for_input"}
                              and str(Path(item["repo"]).resolve()) == repo_key]
                if not switchable:
                    print("No other paused sessions exist for this repository.")
                else:
                    print("Paused sessions you can switch to:")
                    for item in switchable[:10]:
                        print(f"  {item['id']}  {item['status']}  {item['goal']}")
                    print("Use /switch <id>. The current session pauses first.")
                continue
            target = store.get(target_id)
            if target is None or str(Path(target["repo"]).resolve()) != repo_key:
                print(f"No session {target_id!r} exists for this repository. Use /switch to list sessions.")
                continue
            if target["id"] == task["id"]:
                print("You are already in this session.")
                continue
            if target["status"] not in {"paused", "waiting_for_input"}:
                print(f"Session {target_id} is {target['status']}; switch to a paused or waiting session instead.")
                continue
            return value
        return value


def _compact_history(task_id: str, task: dict[str, Any], provider: str, history: list[dict[str, Any]],
                     store: Store, *, force: bool = False,
                     on_status: StatusCallback = None) -> list[dict[str, Any]]:
    size = sum(len(json.dumps(message, ensure_ascii=False)) for message in history)
    if size <= HISTORY_CHAR_LIMIT and not force:
        return history
    _set_status(on_status, "Compacting context")
    print("\n[compacting task context and saving a checkpoint]", flush=True)
    system = """Create a concise, factual checkpoint for a coding task that will continue later. Preserve the user goal, acceptance criteria, repository facts, decisions, modified files, important command results, failures, and remaining work. Do not claim unfinished work is complete. Do not execute tools. Keep the checkpoint under 5,000 characters."""
    result = complete(provider, task["model"], history, [], system, stream_output=False)
    summary = str(result.get("text") or "").strip()
    if not summary:
        raise RuntimeError("Model returned an empty context checkpoint")
    checkpoint = {"role": "user", "content": "Persistent Everrun task checkpoint. Continue from this state and inspect the worktree before editing:\n\n" + summary}
    store.add_message(task_id, provider, checkpoint, checkpoint=True)
    store.event(task_id, "context_compacted", {"previous_chars": size, "checkpoint_chars": len(summary)})
    return store.messages(task_id)


def _switch_target(value: str) -> str | None:
    if value.startswith("/switch "):
        target = value.split(None, 1)[1].strip()
        if target:
            return target
    return None


def _pause_for_switch(task_id: str, target: str, store: Store, on_status: StatusCallback,
                      summary: str | None = None) -> str:
    _set_status(on_status, f"Switching to session {target}")
    if summary is None:
        store.update(task_id, status="paused")
    else:
        store.update(task_id, status="paused", summary=summary[:4000])
    store.event(task_id, "session_switched", {"to": target})
    print(f"Session paused. Switching to session {target} "
          f"(outside the full-screen UI, open it with: everrun resume {target})")
    return target


def _read_interactive_message(task_id: str, task: dict[str, Any], provider: str,
                              history: list[dict[str, Any]], store: Store,
                              on_status: StatusCallback = None) -> tuple[str, list[dict[str, Any]]]:
    while True:
        value = _read_session_message(task, store, provider)
        if value != "/compact":
            return value, history
        _set_status(on_status, "Compacting context")
        history = _compact_history(task_id, task, provider, history, store, force=True,
                                   on_status=on_status)
        print("Conversation checkpoint saved. Continue with your next message.")
        _set_status(on_status, "Waiting for your message")


def _run_task(task_id: str, *, answer: str | None = None, interactive: bool = False,
              store: Store | None = None, on_status: StatusCallback = None,
              extensions: ExtensionSet | None = None) -> str | None:
    _set_status(on_status, "Opening session")
    store = store or Store()
    task = store.get(task_id)
    if not task:
        raise RuntimeError(f"Unknown task {task_id}")
    if task["status"] == "review_required":
        raise RuntimeError("This task is awaiting review. Start a new task for follow-up work.")
    provider = task["provider"]
    if not task["model"]:
        raise RuntimeError("No model selected. Run `everrun models` and start the task with `--model <model>`.")

    if extensions is None:
        extensions = ExtensionSet.load(Path(task["repo"]))
    extension_schemas = extensions.schemas()
    extension_summary = extensions.summary()
    if extension_summary["mcp_tools"] or extension_summary["plugin_tools"] or extension_summary["errors"]:
        store.event(task_id, "extensions_loaded", extension_summary)
    for notice in extensions.notices():
        print(f"[extensions] {notice}", flush=True)
    tool_schemas = TOOL_SCHEMAS + extension_schemas

    sandbox = policy_from_env()
    if sandbox.mode != "off":
        try:
            sandbox.writable.append(common_dir(Path(task["repo"])))
        except Exception:
            pass  # the worktree path alone is still confined

    if refresh_clean_worktree(Path(task["repo"]), Path(task["worktree"]), task["base_commit"]):
        store.event(task_id, "worktree_refreshed_from_checkout", {"base_commit": task["base_commit"]})
        print("Updated the untouched task worktree with the current checkout changes.", flush=True)

    history = _recover_interrupted_tools(task_id, provider, store.messages(task_id), store)
    continuing_session = interactive and _session_is_waiting_for_user(history)
    if not history:
        first = {"role": "user", "content": "Start this task. Inspect the repository and make progress toward the goal.\n\n" + task["goal"]}
        history.append(first)
        store.add_message(task_id, provider, first)
    if answer:
        message = {"role": "user", "content": "Human response to your question: " + answer}
        history.append(message)
        store.add_message(task_id, provider, message)
        store.event(task_id, "input_answered", {"answer": answer})
    elif continuing_session:
        _show_session_history(history)
        if task.get("summary"):
            print(f"Resuming session. Previous status: {task['status']}\n{task['summary']}\n")
        else:
            print("Resuming saved session.\n")
        _set_status(on_status, "Waiting for your message")
        user_input, history = _read_interactive_message(task_id, task, provider, history, store, on_status)
        if user_input in {"/exit", "/quit"}:
            store.update(task_id, status="paused")
            print(f"Session remains paused. Resume with: everrun resume {task_id}")
            return
        switch = _switch_target(user_input)
        if switch:
            return _pause_for_switch(task_id, switch, store, on_status)
        if user_input == "/done":
            summary = task.get("summary") or "Interactive session closed by the user."
            report_path = _write_report(task, task_id, summary, "", store)
            store.update(task_id, status="review_required", summary=summary[:4000])
            store.event(task_id, "finished", {"summary": summary[:4000], "report": str(report_path)})
            print(f"Session ready for review. Report: {report_path}")
            return
        if user_input:
            message = {"role": "user", "content": user_input}
            store.add_message(task_id, provider, message)
            history = store.messages(task_id)
            store.event(task_id, "session_message", {"message": user_input})

    store.update(task_id, status="running")
    store.event(task_id, "model_run_started", {"provider": provider, "model": task["model"]})
    print(f"Everrun task {task_id} running with {provider}/{task['model']} in {task['worktree']}", flush=True)
    transcript: list[str] = []
    for turn in range(MAX_TURNS_PER_INVOCATION):
        try:
            history = _compact_history(task_id, task, provider, history, store, on_status=on_status)
            for attempt in range(2):
                try:
                    _set_status(on_status, f"Thinking · turn {turn + 1}")
                    print("Everrun> ", end="", flush=True)
                    result = complete(provider, task["model"], history, tool_schemas,
                                      _system_prompt(task, interactive=interactive,
                                                     extra_tools=bool(extension_schemas)))
                    if not str(result.get("text") or "").strip() and not result.get("tool_calls"):
                        raise EmptyModelResponse(
                            f"{provider} returned no user-visible text or tool calls.",
                            response_id=result.get("response_id"),
                            usage=result.get("usage"),
                        )
                    break
                except EmptyModelResponse as exc:
                    store.event(task_id, "empty_model_response", {"turn": turn + 1, "attempt": attempt + 1,
                                                                    "provider": provider, "model": task["model"],
                                                                    "response_id": exc.response_id, "usage": exc.usage,
                                                                    "error": str(exc)})
                    if attempt:
                        raise
                    print(f"\n{exc} Retrying this model turn once...", flush=True)
        except KeyboardInterrupt:
            _set_status(on_status, "Paused")
            store.update(task_id, status="paused")
            store.event(task_id, "interrupted", {"turn": turn})
            print(f"\nPaused. Resume with: everrun resume {task_id}")
            return
        except Exception as exc:
            _set_status(on_status, "Paused after error")
            store.update(task_id, status="paused")
            store.event(task_id, "model_error", {"error": str(exc), "turn": turn})
            raise

        text = str(result.get("text") or "")
        if text:
            transcript.append(text)
            if not result.get("streamed"):
                print(text, end="\n", flush=True)
        elif not result.get("streamed"):
            print(flush=True)
        _save_assistant(task_id, provider, result, store)
        calls = result.get("tool_calls", [])
        try:
            if calls:
                _tool_outputs(task_id, task, calls, provider, store, on_status,
                              extensions=extensions, sandbox=sandbox)
        except KeyboardInterrupt:
            _set_status(on_status, "Paused")
            store.update(task_id, status="paused")
            store.event(task_id, "interrupted", {"turn": turn, "during": "tool_execution"})
            print(f"\nPaused. Resume with: everrun resume {task_id}")
            return
        history = store.messages(task_id)
        store.event(task_id, "model_turn", {"turn": turn + 1, "provider": provider, "model": task["model"],
                                             "tool_calls": [call.get("name") for call in calls],
                                             "usage": result.get("usage") or {},
                                             "response_id": result.get("response_id")})

        if calls:
            continue
        if result.get("finish_reason") in {"max_tokens", "length"}:
            continuation = {"role": "user", "content": "Continue from the exact point you stopped. Do not repeat earlier content; keep using tools until the task is complete."}
            store.add_message(task_id, provider, continuation)
            history = store.messages(task_id)
            store.event(task_id, "model_output_continued", {"turn": turn + 1, "reason": result["finish_reason"]})
            continue
        full_text = "\n".join(transcript)
        if "EVERUN_STATUS: WAITING_FOR_INPUT" in text:
            question = text.rsplit("EVERUN_STATUS: WAITING_FOR_INPUT", 1)[-1].strip()
            if interactive:
                print(f"\nEverrun needs your input: {question}")
                _set_status(on_status, "Waiting for your answer")
                user_input, history = _read_interactive_message(task_id, task, provider, history, store, on_status)
                if user_input in {"/exit", "/quit"}:
                    _set_status(on_status, "Paused")
                    store.update(task_id, status="paused")
                    store.event(task_id, "session_paused", {"turn": turn + 1})
                    print(f"Session paused. Resume with: everrun resume {task_id}")
                    return
                switch = _switch_target(user_input)
                if switch:
                    return _pause_for_switch(task_id, switch, store, on_status)
                if user_input == "/done":
                    summary = text.strip() or "Interactive session closed by the user."
                    report_path = _write_report(task, task_id, summary, "\n".join(transcript), store)
                    store.update(task_id, status="review_required", summary=summary[:4000])
                    store.event(task_id, "finished", {"summary": summary[:4000], "report": str(report_path)})
                    _set_status(on_status, "Review required")
                    print(f"Session ready for review. Report: {report_path}")
                    return
                message = {"role": "user", "content": user_input}
                store.add_message(task_id, provider, message)
                history = store.messages(task_id)
                store.event(task_id, "input_answered", {"answer": user_input})
                continue
            store.update(task_id, status="waiting_for_input", summary=question[:1000])
            store.event(task_id, "input_requested", {"question": question[:1000]})
            print(f"Task is waiting for your input. Resume with: everrun resume {task_id} --answer \"...\"")
            return
        if not text.strip() and not calls:
            response_id = result.get("response_id")
            suffix = f" (response {response_id})" if response_id else ""
            message = f"{provider} returned an empty response with no tool calls{suffix}. The session is paused; resume it to retry."
            store.update(task_id, status="paused", summary=message)
            store.event(task_id, "empty_model_response", {"turn": turn + 1, "provider": provider,
                                                           "model": task["model"], "response_id": response_id})
            raise RuntimeError(message)
        if interactive:
            _set_status(on_status, "Waiting for your message")
            user_input, history = _read_interactive_message(task_id, task, provider, history, store, on_status)
            if user_input in {"/exit", "/quit"}:
                _set_status(on_status, "Paused")
                store.update(task_id, status="paused", summary=text.strip()[:4000])
                store.event(task_id, "session_paused", {"turn": turn + 1})
                print(f"Session paused. Resume with: everrun resume {task_id}")
                return
            switch = _switch_target(user_input)
            if switch:
                return _pause_for_switch(task_id, switch, store, on_status, summary=text.strip())
            if user_input == "/done":
                summary = text.strip() or "Interactive session completed."
                report_path = _write_report(task, task_id, summary, "\n".join(transcript), store)
                store.update(task_id, status="review_required", summary=summary[:4000])
                store.event(task_id, "finished", {"summary": summary[:4000], "report": str(report_path)})
                _set_status(on_status, "Review required")
                print(f"Session ready for review. Report: {report_path}")
                return
            if not user_input:
                continue
            message = {"role": "user", "content": user_input}
            store.add_message(task_id, provider, message)
            history = store.messages(task_id)
            store.event(task_id, "session_message", {"message": user_input})
            continue
        summary = text.strip() or "The model completed without returning a text summary."
        report_path = _write_report(task, task_id, summary, full_text, store)
        store.update(task_id, status="review_required", summary=summary[:4000])
        store.event(task_id, "finished", {"summary": summary[:4000], "report": str(report_path)})
        _set_status(on_status, "Review required")
        print(f"Task ready for review. Report: {report_path}")
        return

    store.update(task_id, status="paused")
    store.event(task_id, "turn_limit", {"limit": MAX_TURNS_PER_INVOCATION})
    _set_status(on_status, "Paused at turn limit")
    print(f"Paused after {MAX_TURNS_PER_INVOCATION} model turns. Resume with: everrun resume {task_id}")


def run_task(task_id: str, *, answer: str | None = None, interactive: bool = False,
             on_status: StatusCallback = None) -> str | None:
    """Run a task; in interactive sessions, returns a /switch target session id when one was requested."""
    with _task_lock(task_id):
        store = Store()
        extensions = None
        try:
            task = store.get(task_id)
            if task:
                extensions = ExtensionSet.load(Path(task["repo"]))
            return _run_task(task_id, answer=answer, interactive=interactive, store=store, on_status=on_status,
                             extensions=extensions)
        finally:
            if extensions is not None:
                extensions.close()
            store.close()
