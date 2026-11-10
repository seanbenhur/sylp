from __future__ import annotations

import os
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

    mode: "off" never sandboxes, "auto" sandboxes when a backend exists and
    warns otherwise, "require" refuses to run without a backend.
    network: whether sandboxed commands keep network access.
    writable: extra absolute paths the sandbox may write to (for example the
    repository's common Git directory, which worktree Git commands update).
    """

    mode: str = "auto"
    network: bool = True
    writable: list[Path] = field(default_factory=list)


def policy_from_env(env: dict[str, str] | None = None) -> SandboxPolicy:
    env = os.environ if env is None else env
    mode = (env.get("SYLP_SANDBOX") or "auto").strip().lower()
    if mode not in {"off", "auto", "require"}:
        raise ValueError("SYLP_SANDBOX must be one of: off, auto, require")
    network = (env.get("SYLP_SANDBOX_NETWORK") or "on").strip().lower() not in {"0", "off", "false", "no"}
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


def wrap_command(command: str, root: Path, policy: SandboxPolicy) -> tuple[str, str | None]:
    """Return the command to execute and an optional warning note.

    The note is only set when the caller asked for a sandbox and none was
    applied; an active backend is silent because the approval prompt and
    session output already show it.
    """
    if policy.mode == "off":
        return command, None
    backend = detect_backend()
    argv: list[str] | None = None
    root = root.resolve()
    if backend == "bwrap":
        # The worktree stays writable, the Git common directory is writable so
        # status/commit work, and /tmp stays a shared writable scratch space;
        # everything else is read-only. No --new-session: the sandboxed
        # command must stay in Sylp's process group so timeout and
        # interrupt kills reach it.
        argv = ["bwrap", "--die-with-parent",
                "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts"]
        if not policy.network:
            argv.append("--unshare-net")
        argv += ["--ro-bind", "/", "/"]
        if root != Path("/tmp"):
            argv += ["--bind", "/tmp", "/tmp"]
        for extra in dict.fromkeys(Path(p).resolve() for p in policy.writable):
            if extra != root and extra.exists():
                argv += ["--bind", str(extra), str(extra)]
        argv += ["--bind", str(root), str(root), "--proc", "/proc",
                 "--chdir", str(root), "--", "/bin/bash", "-lc", command]
    elif backend == "sandbox-exec":
        profile = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
        ]
        writable = [root, Path("/tmp"), Path("/private/tmp"), Path("/var/folders")]
        writable.extend(Path(p).resolve() for p in policy.writable)
        subpaths = " ".join(f'(subpath "{path}")' for path in dict.fromkeys(writable))
        profile.append(f"(allow file-write* {subpaths})")
        if not policy.network:
            profile.append("(deny network*)")
        argv = ["sandbox-exec", "-p", "\n".join(profile), "/bin/bash", "-lc", command]
    if argv is not None and _backend_works(argv):
        return shlex.join(argv), None
    if policy.mode == "require":
        raise SandboxUnavailable(
            "SYLP_SANDBOX=require but no working sandbox backend was found. "
            "Install bubblewrap (Linux) or run on macOS with sandbox-exec."
        )
    reason = "no sandbox backend found" if argv is None else f"{backend} failed its probe"
    return command, (f"Warning: {reason}; this command ran WITHOUT sandboxing. "
                     "Install bubblewrap (Linux) to confine commands.")
