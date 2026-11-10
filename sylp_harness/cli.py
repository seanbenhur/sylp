from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import __version__
from .auth import login_antigravity, login_openai
from .git import prepare_worktree
from .providers import (
    PROVIDERS,
    configure_provider,
    default_model,
    default_provider,
    is_connected,
    list_models,
    load_auth,
    remove_key,
    save_default_model,
    save_default_provider,
)
from .runner import run_task, task_home
from .store import Store
from .terminal import TerminalUI


def _git_root(repo: Path) -> Path:
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(f"Not inside a Git repository: {repo}")
    return Path(p.stdout.strip()).resolve()


def _provider_name(value: str) -> str:
    if value not in PROVIDERS:
        raise argparse.ArgumentTypeError(f"Choose one of: {', '.join(PROVIDERS)}")
    return value


def _interactive() -> argparse.Namespace:
    repo = _git_root(Path.cwd())
    existing = [item for item in Store().list()
                if Path(item["repo"]).resolve() == repo
                and item["status"] in {"paused", "running", "waiting_for_input"}]
    print("Coding session")
    print(f"Repository: {repo}")
    print("Type /exit at the prompt to pause this session.\n")
    if existing:
        print("Saved sessions:")
        for item in existing[:10]:
            print(f"  {item['id']}  {item['status']}  {item['goal']}")
        selected = input("Resume a session by ID, or press Enter for a new one: ").strip()
        if selected:
            match = next((item for item in existing if item["id"] == selected), None)
            if not match:
                raise RuntimeError(f"No resumable session {selected!r} for this repository")
            return argparse.Namespace(command="resume", task_id=selected, answer=None, model=None,
                                      batch=False)
    configured_provider = os.environ.get("SYLP_PROVIDER") or default_provider()
    provider = configured_provider
    if not provider:
        connected = [candidate for candidate in PROVIDERS if is_connected(candidate)]
        if len(connected) == 1:
            provider = connected[0]
        elif len(connected) > 1:
            print("Connected providers:")
            for candidate in connected:
                print(f"  {candidate}")
            while True:
                provider = input(f"Provider [{connected[0]}]: ").strip() or connected[0]
                if provider in connected:
                    break
                print(f"Choose one of: {', '.join(connected)}")
        else:
            provider = "openai"
    if provider not in PROVIDERS:
        raise RuntimeError(f"Unknown SYLP_PROVIDER {provider!r}; choose from {', '.join(PROVIDERS)}")
    if not is_connected(provider):
        if provider == "openai":
            action = "sylp auth login openai"
        elif provider == "antigravity":
            action = "sylp auth login antigravity"
        else:
            action = f"sylp auth set {provider}"
        raise RuntimeError(f"No {provider} account is connected. Connect it with `{action}`, then start Sylp again.")

    model = os.environ.get("SYLP_MODEL") or default_model(provider)
    if not model:
        available = list_models(provider)
        if not available:
            raise RuntimeError(f"No models are available from {provider}.")
        print("Available models:")
        for item in available[:20]:
            print(f"  {item['id']}  ({item['name']})")
        model = input(f"\nModel [{available[0]['id']}]: ").strip() or available[0]["id"]
        save_default_model(provider, model)
    if not configured_provider:
        save_default_provider(provider)
    print(f"Using {provider}/{model}.\n")
    return argparse.Namespace(command="chat", goal=None,
                              repo=str(repo), accept=[], provider=provider, model=model,
                              auto_approve=os.environ.get("SYLP_AUTO_APPROVE", "").lower()
                              in {"1", "true", "yes"})


def _main(*, ui: TerminalUI | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sylp", description="A persistent coding harness powered directly by model providers")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("run", help="Start a model-driven coding task")
    start.add_argument("goal", help="Task goal")
    start.add_argument("--repo", default=".", help="Git repository (default: current directory)")
    start.add_argument("--accept", action="append", default=[], help="Acceptance criterion; repeat as needed")
    start.add_argument("--provider", type=_provider_name, default=os.environ.get("SYLP_PROVIDER", "openai"), help="Model provider")
    start.add_argument("--model", default=os.environ.get("SYLP_MODEL"), help="Model ID from `sylp models`")
    start.add_argument("--auto-approve", action="store_true", help="Run shell tool calls without asking each time")

    resume = sub.add_parser("resume", help="Reopen a paused persistent coding session")
    resume.add_argument("task_id")
    resume.add_argument("--answer", help="Answer the task's pending question")
    resume.add_argument("--model", help="Switch to a model from the same provider")
    resume.add_argument("--batch", action="store_true", help="Resume autonomously until completion or another pause")

    models = sub.add_parser("models", help="List models available from a connected provider")
    models.add_argument("--provider", type=_provider_name, default=os.environ.get("SYLP_PROVIDER", "openai"))

    auth = sub.add_parser("auth", help="Connect or disconnect a model provider")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", help="Sign in with ChatGPT or Antigravity for model usage")
    login.add_argument("provider", choices=["openai", "antigravity"])
    set_key = auth_sub.add_parser("set", help="Save a provider API key")
    set_key.add_argument("provider", type=_provider_name)
    set_key.add_argument("--base-url", help="Required for openai-compatible providers")
    auth_sub.add_parser("status", help="Show connected providers")
    logout = auth_sub.add_parser("logout", help="Remove saved credentials for a provider")
    logout.add_argument("provider", type=_provider_name)

    status = sub.add_parser("status", help="Show task state")
    status.add_argument("task_id", nargs="?")
    report = sub.add_parser("report", help="Print the final review report")
    report.add_argument("task_id")
    sub.add_parser("list", help="List tasks")
    mcp_cmd = sub.add_parser("mcp", help="List configured MCP servers and probe their tools")
    mcp_cmd.add_argument("--repo", default=".", help="Repository whose .sylp/mcp.json applies (default: current directory)")
    plugins_cmd = sub.add_parser("plugins", help="List discovered Sylp plugins and their tools")
    plugins_cmd.add_argument("--repo", default=".", help="Repository whose .sylp/plugins/ applies (default: current directory)")
    sandbox_cmd = sub.add_parser("sandbox", help="Show the shell sandbox status")
    sandbox_cmd.add_argument("--test", action="store_true", help="Also run a probe command through the sandbox")
    memory_cmd = sub.add_parser("memory", help="List, search, or forget durable memories")
    memory_cmd.add_argument("--repo", default=".", help="Repository whose memories apply (default: current directory)")
    memory_cmd.add_argument("--all", action="store_true", help="Include memories from every repository")
    memory_cmd.add_argument("--search", help="Search memories with the retrieval pipeline")
    memory_cmd.add_argument("--forget", type=int, metavar="ID", help="Delete one memory")
    args = parser.parse_args()
    if args.command is None:
        try:
            args = _interactive()
        except Exception as exc:
            print(f"sylp: {exc}", file=sys.stderr)
            raise SystemExit(1)

    store = Store()
    try:
        if args.command == "run":
            if not args.model:
                raise RuntimeError("Choose a model with `sylp models --provider <provider>`, then pass `--model <model-id>`")
            if not is_connected(args.provider):
                if args.provider == "openai":
                    raise RuntimeError("Connect with `sylp auth login openai` or save an API key with `sylp auth set openai`")
                if args.provider == "antigravity":
                    raise RuntimeError("Connect with `sylp auth login antigravity` or save an API key with `sylp auth set antigravity`")
                raise RuntimeError(f"Connect with `sylp auth set {args.provider}` before starting a task")
            repo = _git_root(Path(args.repo).resolve())
            task_id = uuid.uuid4().hex[:10]
            worktree, base = prepare_worktree(repo, task_id)
            store.db.execute(
                "INSERT INTO tasks(id,goal,acceptance,repo,worktree,base_commit,status,model,provider,auto_approve,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%s','now'),strftime('%s','now'))",
                (task_id, args.goal, json.dumps(args.accept), str(repo), str(worktree), base,
                 "running", args.model, args.provider, int(args.auto_approve)),
            )
            store.db.commit()
            store.event(task_id, "created", {"goal": args.goal, "acceptance": args.accept, "base_commit": base,
                                               "provider": args.provider, "model": args.model})
            print(f"Created task {task_id}")
            run_task(task_id)
        elif args.command == "chat":
            repo = _git_root(Path(args.repo).resolve())

            def start_session() -> None:
                if not args.goal:
                    while True:
                        prompt = input("You> ").strip()
                        if prompt == "/help":
                            print("Type a coding request to start. In a session: /help, /exit, /done.")
                            continue
                        if prompt == "/exit":
                            print("No session started.")
                            return
                        if prompt:
                            args.goal = prompt
                            break
                        print("Enter a message, or use /exit to leave.")
                task_id = uuid.uuid4().hex[:10]
                worktree, base = prepare_worktree(repo, task_id)
                store.db.execute(
                    "INSERT INTO tasks(id,goal,acceptance,repo,worktree,base_commit,status,model,provider,auto_approve,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%s','now'),strftime('%s','now'))",
                    (task_id, args.goal, json.dumps(args.accept), str(repo), str(worktree), base,
                     "running", args.model, args.provider, int(args.auto_approve)),
                )
                store.db.commit()
                store.event(task_id, "created", {"goal": args.goal, "acceptance": args.accept,
                                                   "base_commit": base, "provider": args.provider,
                                                   "model": args.model, "interactive": True})
                print(f"Session {task_id} · {args.provider}/{args.model}")
                print(f"Working in {worktree}. Use /help for session commands.\n")
                current_id = task_id
                while True:
                    if ui is not None:
                        ui.bind_session(repo, current_id)
                    switch_to = run_task(current_id, interactive=True, on_status=ui.set_status if ui else None)
                    if not switch_to:
                        break
                    next_task = store.get(switch_to)
                    if ui is None or next_task is None:
                        print(f"Open the session with: sylp resume {switch_to}")
                        break
                    current_id = switch_to
                    ui.title = f"{repo.name} · {next_task['provider']}/{next_task['model']}"
                    print(f"\nSwitched to session {current_id} · {next_task['goal']}\n")

            if sys.stdin.isatty() and sys.stdout.isatty():
                if ui is None:
                    TerminalUI(f"{repo.name} · {args.provider}/{args.model}").run(start_session)
                else:
                    ui.title = f"{repo.name} · {args.provider}/{args.model}"
                    start_session()
            else:
                start_session()
        elif args.command == "resume":
            task = store.get(args.task_id)
            if not task:
                raise RuntimeError(f"Unknown task {args.task_id}")
            if args.model:
                store.update(args.task_id, model=args.model)
            def resume_session() -> None:
                current_id = args.task_id
                answer = args.answer
                while True:
                    current = store.get(current_id)
                    if ui is not None and current is not None:
                        ui.bind_session(Path(current["repo"]), current_id)
                    switch_to = run_task(current_id, answer=answer, interactive=not args.batch,
                                         on_status=ui.set_status if ui else None)
                    answer = None
                    if args.batch or not switch_to:
                        break
                    next_task = store.get(switch_to)
                    if ui is None or next_task is None:
                        print(f"Open the session with: sylp resume {switch_to}")
                        break
                    current_id = switch_to
                    ui.title = f"{Path(next_task['repo']).name} · {next_task['provider']}/{next_task['model']}"
                    print(f"\nSwitched to session {current_id} · {next_task['goal']}\n")

            if not args.batch and sys.stdin.isatty() and sys.stdout.isatty():
                if ui is None:
                    TerminalUI(f"{Path(task['repo']).name} · {task['provider']}/{task['model']}").run(resume_session)
                else:
                    ui.title = f"{Path(task['repo']).name} · {task['provider']}/{task['model']}"
                    resume_session()
            else:
                resume_session()
        elif args.command == "models":
            for model in list_models(args.provider):
                print(f"{model['id']}\t{model['name']}")
        elif args.command == "auth":
            if args.auth_command == "login":
                if args.provider == "openai":
                    login_openai()
                elif args.provider == "antigravity":
                    login_antigravity()
            elif args.auth_command == "set":
                base_url = args.base_url
                if args.provider == "openai-compatible":
                    if not base_url:
                        base_url = input("OpenAI-compatible base URL: ").strip()
                    api_key = getpass.getpass("API key (leave blank if not required): ")
                elif args.provider in ("gemini", "antigravity"):
                    print("Tip: Get a free Gemini API key at https://aistudio.google.com/apikey")
                    api_key = getpass.getpass("Gemini / Antigravity API key: ")
                else:
                    api_key = getpass.getpass(f"{args.provider} API key: ")
                configure_provider(args.provider, api_key=api_key or None, base_url=base_url)
                print(f"Connected {args.provider}.")
            elif args.auth_command == "status":
                for provider in PROVIDERS:
                    if provider == "openai":
                        saved = load_auth().get("openai", {})
                        if os.environ.get("OPENAI_API_KEY"):
                            detail = "connected (OPENAI_API_KEY)"
                        elif saved.get("auth_type") == "oauth":
                            detail = f"connected with ChatGPT{': ' + saved['email'] if saved.get('email') else ''}"
                        elif saved.get("api_key"):
                            detail = "connected (saved API key)"
                        else:
                            detail = "not connected"
                        print(f"{provider}: {detail}")
                    elif provider == "antigravity":
                        saved = load_auth().get("antigravity", {})
                        from .auth import _antigravity_token_file
                        token_file = _antigravity_token_file()
                        if os.environ.get("ANTIGRAVITY_API_KEY"):
                            detail = "connected (ANTIGRAVITY_API_KEY)"
                        elif os.environ.get("GEMINI_API_KEY"):
                            detail = "connected (GEMINI_API_KEY)"
                        elif os.environ.get("GOOGLE_API_KEY"):
                            detail = "connected (GOOGLE_API_KEY)"
                        elif saved.get("auth_type") == "oauth":
                            detail = f"connected with Antigravity{': ' + saved['email'] if saved.get('email') else ''}"
                        elif token_file.exists():
                            detail = "connected with Antigravity (IDE session)"
                        elif saved.get("api_key"):
                            detail = "connected (saved API key)"
                        elif load_auth().get("gemini", {}).get("api_key"):
                            detail = "connected (saved Gemini API key)"
                        else:
                            detail = "not connected"
                        print(f"{provider}: {detail}")
                    elif provider == "gemini":
                        saved = load_auth().get("gemini", {})
                        if os.environ.get("GEMINI_API_KEY"):
                            detail = "connected (GEMINI_API_KEY)"
                        elif os.environ.get("GOOGLE_API_KEY"):
                            detail = "connected (GOOGLE_API_KEY)"
                        elif saved.get("api_key"):
                            detail = "connected (saved API key)"
                        else:
                            detail = "not connected"
                        print(f"{provider}: {detail}")
                    else:
                        print(f"{provider}: {'connected' if is_connected(provider) else 'not connected'}")
            elif args.auth_command == "logout":
                remove_key(args.provider)
                print(f"Removed saved {args.provider} credentials.")
        elif args.command == "status":
            tasks = [store.get(args.task_id)] if args.task_id else store.list()
            for task in tasks:
                if not task:
                    raise RuntimeError("Task not found")
                if args.task_id:
                    print(f"{task['id']}  {task['status']}  {task['goal']}\nprovider: {task['provider']}  model: {task['model']}\nrepo: {task['repo']}\nworktree: {task['worktree']}\nbase: {task['base_commit']}")
                    for event in store.events(task["id"])[-8:]:
                        print(f"  {event['kind']}: {event['payload']}")
                else:
                    print(f"{task['id']}  {task['status']}  {task['provider']}/{task['model']}  {task['goal']}  [{task['repo']}]")
        elif args.command == "list":
            for task in store.list():
                print(f"{task['id']}  {task['status']}  {task['provider']}/{task['model']}  {task['goal']}")
        elif args.command == "report":
            path = task_home(args.task_id) / "report.md"
            if not path.exists():
                raise RuntimeError(f"No report exists for {args.task_id}")
            print(path.read_text(encoding="utf-8"))
        elif args.command == "mcp":
            from . import mcp as mcp_module, trust
            from .extensions import state_home
            home = state_home()
            repo = Path(args.repo).resolve()
            if not trust.ensure_repo_trusted(home, repo):
                print("Repository extensions are not trusted; skipping .sylp/mcp.json.")
                repo = None
            config = mcp_module.load_config(home, repo)
            if not config:
                print("No MCP servers configured. See docs/extensions.md for the mcp.json format.")
            manager = mcp_module.MCPManager.start(config)
            try:
                for name in sorted(config):
                    if name not in manager.servers:
                        error = next((e for e in manager.errors if e.startswith(f"{name}:")), "failed to start")
                        print(f"{name}: unavailable - {error}")
                        continue
                    prefix = mcp_module.qualify(name, "")
                    tools = [schema["name"] for schema in manager.schemas()
                             if schema["name"].startswith(prefix)]
                    print(f"{name}: {len(tools)} tools")
                    for tool in tools:
                        print(f"  {tool}")
            finally:
                manager.close()
        elif args.command == "plugins":
            from . import trust
            from .extensions import state_home
            from .plugins import PluginRegistry, discover
            home = state_home()
            repo = Path(args.repo).resolve()
            if not trust.ensure_repo_trusted(home, repo):
                print("Repository extensions are not trusted; skipping .sylp/plugins/.")
                repo = None
            registry = PluginRegistry().load(home, repo)
            files = discover(home, repo)
            if not files:
                print("No plugins found. Add .py files to ~/.sylp/plugins/ or <repo>/.sylp/plugins/.")
            for schema in registry.schemas():
                print(f"{schema['name']}\t{schema['description']}")
            for error in registry.errors:
                print(f"error: {error}", file=sys.stderr)
            if registry.errors:
                raise SystemExit(1)
        elif args.command == "memory":
            from . import memory as memory_module
            if args.forget is not None:
                if store.delete_memory(args.forget):
                    print(f"Forgot memory {args.forget}.")
                else:
                    raise RuntimeError(f"No memory {args.forget}")
            elif args.all:
                for item in store.list_memories(include_all=True):
                    scope = "user" if item["repo"] is None else Path(item["repo"]).name
                    print(f"{item['id']}\t[{item['kind']}]\t{scope}\t{item['content']}")
            else:
                repo = str(_git_root(Path(args.repo).resolve()))
                if args.search:
                    hits = memory_module.recall(store, repo, args.search)
                    if not hits:
                        print("No matching memories.")
                    for item in hits:
                        scope = "user" if item["repo"] is None else Path(item["repo"]).name
                        print(f"{item['id']}\t[{item['kind']}]\t{scope}\t{item['content']}")
                else:
                    items = store.list_memories(repo)
                    if not items:
                        print("No memories saved for this repository yet.")
                    for item in items:
                        scope = "user" if item["repo"] is None else Path(item["repo"]).name
                        print(f"{item['id']}\t[{item['kind']}]\t{scope}\t{item['content']}")
        elif args.command == "sandbox":
            from .sandbox import describe, policy_from_env, wrap_command
            policy = policy_from_env()
            print(f"mode: {policy.mode}\nbackend: {describe(policy)}")
            if args.test:
                command, applied = wrap_command("echo sylp-sandbox-ok", Path.cwd(), policy)
                result = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=30)
                if not applied:
                    print("no working sandbox backend; the probe ran WITHOUT sandboxing")
                print(result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}")
        elif args.command == "quit":
            print("No prompt entered; no session started.")
    except Exception as exc:
        print(f"sylp: {exc}", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    # Keep the startup flow (saved sessions, provider/model setup, and chat)
    # inside one full-screen application instead of dropping back to the shell
    # between questions and the coding session.
    if len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        ui = TerminalUI("Coding session")

        def launch() -> None:
            try:
                _main(ui=ui)
            except SystemExit as exc:
                if exc.code:
                    ui.write("\nPress Enter to close. ")
                    ui.input("")

        ui.run(launch)
        return
    _main()


if __name__ == "__main__":
    main()
