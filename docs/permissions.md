# Permissions

Every tool call the model makes passes a deterministic permission engine
before it runs. Each call is **allowed**, **asked** (the user approves or
denies at a prompt), or **denied** outright. No model output participates in
these decisions, and there is no LLM auto-approval path.

## Defaults

| Call | Default |
| --- | --- |
| `list_files`, `read_file`, `search_files`, `git_status`, `git_diff` | allow |
| `write_file`, `apply_patch` inside the worktree | allow |
| Reads or writes of credential-looking files (`.env`, `*.pem`, `id_rsa*`, ...) | ask (never auto-approved) |
| Anything touching `.git/` | deny |
| Writes under `.sylp/` (policy, plugins, MCP config) | deny |
| `run_command` with a known read-only form (`ls`, `cat`, `rg`, `git status`/`diff`/`log`/...) | allow |
| `run_command` with any other or compound shell form | ask |
| MCP and plugin tools | ask (never auto-approved) |

Shell commands are parsed conservatively: only a single simple argv whose
program is a known read-only binary auto-approves. Pipes, redirection,
substitution, `&&`/`;`, or an unknown program all ask. A wrong guess here
always errs toward asking.

`--auto-approve` answers routine asks (ordinary shell commands). It never
answers sensitive asks: credential files, extension tools, and unsandboxed
execution still require a person. When the session cannot prompt (no TTY),
an ask becomes a denial with a message explaining how to allow the action.

## Policy files

- `~/.sylp/permissions.json` - the user's policy.
- `<repo>/.sylp/permissions.json` - the repository's policy.

```json
{
  "rules": [
    {"tool": "run_command", "command": "make test", "decision": "allow"},
    {"tool": "mcp__filesystem__*", "decision": "ask"},
    {"tool": "write_file", "path": "docs/**", "decision": "allow"},
    {"tool": "*", "decision": "deny", "reason": "lock the harness down"}
  ]
}
```

- `tool`: glob matched against the tool name (`run_command`, `mcp__*`).
- `command`: `run_command` only; prefix match against the shell command.
- `path`: glob matched against the worktree-relative path or its basename.

Precedence:

1. Built-in protections (the `.git`/`.sylp` denies) cannot be overridden by
   anyone.
2. A matching user rule decides. The repository cannot weaken it.
3. A matching repository rule can only make the built-in default *stricter*
   (allow -> ask -> deny), never looser: a repo cannot grant itself
   auto-approval.
4. Within one file, the strictest matching rule wins.

An invalid rule fails loudly at session start instead of being ignored.

## Repository extension trust

Plugins and MCP servers a repository supplies (`.sylp/plugins/`,
`.sylp/mcp.json`) run with Sylp's full privileges, so they load only after
you approve their exact contents. The first time a session meets them, Sylp
shows the plugin files and MCP server commands and asks; approval is recorded
in `~/.sylp/trusted.json` and any later change to those files asks again.
Non-interactive sessions skip untrusted repository extensions with a notice.
Files under your own `~/.sylp/` need no approval - only you can put them
there.

## Sandbox

Approved shell commands are also confined by the OS sandbox; see
[Extensions](extensions.md). The sandbox and the permission engine are
separate layers: the engine decides whether a call may run at all, the
sandbox limits what an approved command can touch.
