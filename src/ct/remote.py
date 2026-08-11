"""Every remote call goes through here: plain `ssh` subprocesses.

Shelling out to the system ssh (rather than a Python ssh library) is deliberate:
~/.ssh/config keeps working untouched — keys, certificates, ForwardAgent, ProxyJump.
"""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .config import CtError

# CT_SSH=echo turns every remote call into a printed dry run.
SSH = os.environ.get("CT_SSH", "ssh")

# Fail fast instead of hanging on a prompt, and don't let one dead host stall a fan-out.
BATCH = ["BatchMode=yes", "ConnectTimeout=5"]
# Multiplexing, without asking the user to edit ~/.ssh/config.
MUX = ["ControlMaster=auto", "ControlPath=~/.ssh/ct-%C", "ControlPersist=10m"]

# Anything interpolated unquoted into a remote command must match this: it keeps `~`
# and `$USER` expandable by the remote shell while ruling out spaces and metacharacters.
SAFE = re.compile(r"[A-Za-z0-9._/~$@-]+")


def token(value, what="value"):
    """Validate a string that will be interpolated unquoted into a remote command."""
    if not SAFE.fullmatch(value or ""):
        raise CtError(f"unsupported {what}: {value!r} (no spaces or shell characters)")
    return value


def _argv(tty=False):
    argv = [SSH]
    if tty:
        argv.append("-t")
    for opt in (MUX if tty else BATCH + MUX):
        argv += ["-o", opt]
    return argv


def run(alias, cmd):
    """Run cmd on alias. Never raises — inspect .returncode."""
    return subprocess.run(
        [*_argv(), alias, cmd], capture_output=True, text=True
    )


def fanout(aliases, cmd_fn):
    """{alias: CompletedProcess}, run in parallel."""
    aliases = list(aliases)
    if not aliases:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda a: run(a, cmd_fn(a)), aliases))
    return dict(zip(aliases, results))


def stream(alias, cmd, tty=False):
    """Run with stdio inherited (tail -f, interactive shells). Returns the exit code."""
    return subprocess.call([*_argv(tty=tty), alias, cmd])


def error(result):
    """The most useful one-line explanation of a failed CompletedProcess."""
    if result.returncode == 255 and not result.stdout:
        return "unreachable"
    lines = [l.strip() for l in (result.stderr or "").splitlines() if l.strip()]
    return lines[-1] if lines else f"exit {result.returncode}"
