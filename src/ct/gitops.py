"""Local git in the control-plane clone, plus the command clusters run to sync."""

from __future__ import annotations

import subprocess

from pathlib import Path

from . import remote
from .config import PROJECT_FILE, CtError

SBATCH_EXT = ".sbatch"


def git(root, *args, check=True):
    r = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if check and r.returncode != 0:
        raise CtError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


# --- reads ---------------------------------------------------------------


def fetch(root):
    git(root, "fetch", "--quiet", "origin")


def current_branch(root):
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")


def origin_url(root):
    return git(root, "remote", "get-url", "origin")


def sha(root, ref):
    return git(root, "rev-parse", ref)


def describe(root, ref):
    """e.g. 'a3f1c02 add lr sweep (6 minutes ago)'"""
    return git(root, "log", "-1", "--format=%h %s (%cr)", ref)


def dirty(root, pathspec="."):
    return bool(git(root, "status", "--porcelain", "--", pathspec))


def ahead(root, branch):
    n = git(root, "rev-list", "--count", f"origin/{branch}..HEAD", check=False)
    return int(n) if n.isdigit() else 0


def slurm_dirs(root, ref):
    """Directory names directly under slurm/ on ref."""
    out = git(root, "ls-tree", "-d", "--name-only", f"{ref}:slurm", check=False)
    return sorted(name.strip("/") for name in out.splitlines() if name.strip())


def sbatch_files(root, ref, subdir):
    """[(blob, path)] for *.sbatch under slurm/<subdir>/ on ref.

    Reading from a ref rather than the working tree is the point: it lists exactly
    what a cluster will have after it pulls, even if this clone is stale.
    """
    out = git(root, "ls-tree", "-r", ref, "--", f"slurm/{subdir}/", check=False)
    files = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and path.endswith(SBATCH_EXT):
            files.append((parts[2], path))
    return sorted(files, key=lambda bp: bp[1])


# --- writes --------------------------------------------------------------


def ensure_ignored(root, name=PROJECT_FILE):
    """Guarantee that `name` is git-ignored, adding it to .gitignore if it is not.

    Checked before every `git add -A`, not only at init: .ct.toml holds the repo path on
    each cluster, which contains your username. A `reset --hard`, rebase or branch switch
    can remove the ignore rule, and the next push would then publish the file.
    """
    if subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", name],
        capture_output=True,
    ).returncode == 0:
        raise CtError(
            f"{name} is committed in this repo — it holds cluster paths that contain "
            f"your username. Untrack it first:  git rm --cached {name}"
        )
    if subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", name], capture_output=True
    ).returncode == 0:
        return False
    f = Path(root) / ".gitignore"
    lines = f.read_text().splitlines() if f.exists() else []
    with f.open("a") as fh:
        fh.write(("" if not lines or lines[-1] == "" else "\n") + name + "\n")
    return True


def push(root, message):
    """Commit anything outstanding and push the current branch. Returns the branch."""
    branch = current_branch(root)
    if ensure_ignored(root):
        git(root, "add", "--", ".gitignore")
    if dirty(root):
        git(root, "add", "-A")
        git(root, "commit", "-m", message)
    git(root, "push", "--set-upstream", "origin", branch)
    return branch


def pull(root):
    """Fast-forward the current branch from origin, whatever upstream config says."""
    git(root, "pull", "--ff-only", "origin", current_branch(root))


# --- the remote sync chain ----------------------------------------------


def sync_cmd(path, branch):
    """Bring a cluster clone exactly to origin/<branch> and echo its HEAD.

    The explicit checkout matters: a bare `git pull` would update whichever branch the
    cluster clone happens to sit on. --ff-only always — a merge commit created on a
    cluster would mean it is no longer running what is on origin.
    """
    p = remote.token(path, "repo path")
    b = remote.token(branch, "branch")
    return (
        f"cd {p} && git fetch --quiet origin && "
        f"(git checkout --quiet {b} 2>/dev/null || "
        f"git checkout --quiet -b {b} --track origin/{b}) && "
        f"git merge --ff-only --quiet origin/{b} && git rev-parse HEAD"
    )


def probe_cmd(path):
    """Report `sbatch` availability and the clone's HEAD on a cluster."""
    p = remote.token(path, "repo path")
    return (
        'command -v sbatch >/dev/null && echo sbatch=yes || echo sbatch=no; '
        f'if [ -d {p}/.git ]; then '
        f'echo "head=$(git -C {p} log -1 --abbrev-commit --pretty=oneline 2>/dev/null '
        '|| echo no-commits)"; '
        'else echo head=missing; fi'
    )


def find_clone_cmd(name):
    """Echo the first conventional location where this repo is already cloned."""
    n = remote.token(name, "project name")
    candidates = f"~/{n} ~/work/{n} /scratch/$USER/{n}"
    return f'for p in {candidates}; do [ -d "$p/.git" ] && echo "$p" && break; done'
