from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class SandboxPolicy:
    """How shell commands should be confined.

    mode: "off" never sandboxes, "auto" sandboxes when a backend works and
    otherwise makes the caller ask the user before running unsandboxed,
    "require" refuses to run without a backend.
    network: whether sandboxed commands keep network access. Off by default.
    writable: extra absolute paths the sandbox may write to (for example the
    repository's common Git directory, which worktree Git commands update).
    scratch: private owner-only scratch directory bound in as /tmp instead of
    sharing the host's world-writable /tmp with the command.
    """

    mode: str = "auto"
    network: bool = False
    writable: list[Path] = field(default_factory=list)
    scratch: Path | None = None


def policy_from_env(env: dict[str, str] | None = None) -> SandboxPolicy:
    env = os.environ if env is None else env
    mode = (env.get("SYLP_SANDBOX") or "auto").strip().lower()
    if mode not in {"off", "auto", "require"}:
        raise ValueError("SYLP_SANDBOX must be one of: off, auto, require")
    network = (env.get("SYLP_SANDBOX_NETWORK") or "off").strip().lower() not in {"0", "off", "false", "no"}
    return SandboxPolicy(mode=mode, network=network)


def detect_backend() -> str | None:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if shutil.which("bwrap"):
        return "bwrap"
    return None


def describe(policy: SandboxPolicy) -> str:
    if policy.mode == "off":
        return "off"
    backend = detect_backend()
    if backend is None:
        return "unavailable"
    return f"{backend} (network {'on' if policy.network else 'off'})"


def scratch_dir(policy: SandboxPolicy) -> Path:
    """The private scratch directory a sandboxed command sees as /tmp."""
    scratch = policy.scratch
    if scratch is None:
        from .extensions import state_home
        scratch = state_home() / "scratch"
    scratch = Path(scratch)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        scratch.chmod(0o700)
    except OSError:
        pass
    return scratch


_SECRET_NAME = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)")
# Names that match the pattern but are safe to pass along.
_SCRUB_KEEP = {"SSH_AUTH_SOCK"}


def scrubbed_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without variables whose names look like
    credentials (API keys, tokens, secrets, passwords). Children invoked by
    the model never inherit the host's OPENAI_API_KEY, GITHUB_TOKEN, and so
    on. List extra names in SYLP_ENV_ALLOW (comma-separated) to pass specific
    variables through explicitly."""
    env = dict(os.environ if env is None else env)
    keep = _SCRUB_KEEP | {name.strip() for name in env.get("SYLP_ENV_ALLOW", "").split(",")
                          if name.strip()}
    return {name: value for name, value in env.items()
            if name in keep or not _SECRET_NAME.search(name)}


def sandbox_env(policy: SandboxPolicy) -> dict[str, str]:
    """Environment overrides a sandboxed command needs on top of scrubbed_env."""
    if policy.mode == "off":
        return {}
    if detect_backend() == "sandbox-exec":
        # No namespace to remap /tmp in, so point temp users at the scratch dir.
        return {"TMPDIR": str(scratch_dir(policy))}
    return {}


# Probe results, keyed by the probe argv. A bwrap binary on a kernel that
# forbids user namespaces detects fine but cannot run anything, so a cheap
# one-time probe decides whether the backend is usable.
_PROBE_CACHE: dict[tuple[str, ...], bool] = {}


def _backend_works(argv: list[str]) -> bool:
    probe = argv[:-1] + ["true"]
    key = tuple(probe)
    if key not in _PROBE_CACHE:
        try:
            result = subprocess.run(probe, capture_output=True, timeout=15)
            _PROBE_CACHE[key] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _PROBE_CACHE[key] = False
    return _PROBE_CACHE[key]


def wrap_command(command: str, root: Path, policy: SandboxPolicy) -> tuple[str, bool]:
    """Return the command to execute and whether sandbox confinement was applied.

    A False result only happens in "auto" mode with no working backend: the
    caller must obtain explicit user approval before running the returned
    (unwrapped) command, because Sylp never falls back to unsandboxed
    execution on its own. "require" mode raises SandboxUnavailable instead.
    """
    if policy.mode == "off":
        return command, True
    backend = detect_backend()
    argv: list[str] | None = None
    root = root.resolve()
    if backend == "bwrap":
        # The worktree stays writable, the Git common directory is writable so
        # status/commit work, and the private scratch directory becomes /tmp;
        # everything else is read-only. No --new-session: the sandboxed
        # command must stay in Sylp's process group so timeout and
        # interrupt kills reach it.
        scratch = scratch_dir(policy)
        argv = ["bwrap", "--die-with-parent",
                "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts"]
        if not policy.network:
            argv.append("--unshare-net")
        argv += ["--ro-bind", "/", "/"]
        if root != Path("/tmp"):
            argv += ["--bind", str(scratch), "/tmp", "--setenv", "TMPDIR", "/tmp"]
        for extra in dict.fromkeys(Path(p).resolve() for p in policy.writable):
            if extra != root and extra.exists():
                argv += ["--bind", str(extra), str(extra)]
        argv += ["--bind", str(root), str(root), "--proc", "/proc",
                 "--chdir", str(root), "--", "/bin/bash", "-lc", command]
    elif backend == "sandbox-exec":
        scratch = scratch_dir(policy)
        profile = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
        ]
        writable = [root, scratch]
        writable.extend(Path(p).resolve() for p in policy.writable)
        subpaths = " ".join(f'(subpath "{path}")' for path in dict.fromkeys(writable))
        profile.append(f"(allow file-write* {subpaths})")
        if not policy.network:
            profile.append("(deny network*)")
        argv = ["sandbox-exec", "-p", "\n".join(profile), "/bin/bash", "-lc", command]
    if argv is not None and _backend_works(argv):
        return shlex.join(argv), True
    if policy.mode == "require":
        raise SandboxUnavailable(
            "SYLP_SANDBOX=require but no working sandbox backend was found. "
            "Install bubblewrap (Linux) or run on macOS with sandbox-exec."
        )
    return command, False
