"""Global config, per-project .ct.toml, and state file locations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import tomli_w


class CtError(Exception):
    """An expected failure: printed as one red line, no traceback."""


def _env_path(var, default):
    value = os.environ.get(var)
    return Path(value).expanduser() if value else default


CONFIG_PATH = _env_path("CT_CONFIG", Path.home() / ".config" / "ct" / "config.toml")
STATE_DIR = _env_path("CT_STATE", Path.home() / ".local" / "state" / "ct")
PROJECT_FILE = ".ct.toml"


# --- global config -------------------------------------------------------


@dataclass
class Global:
    clusters: list  # SLURM hosts: the fan-out set and the universe of targets
    workstations: list  # plain ssh hosts, reachable via `ct sh` only
    projects: dict  # name -> local clone path

    @property
    def hosts(self):
        return self.clusters + self.workstations


def load_global():
    if not CONFIG_PATH.exists():
        return Global([], [], {})
    data = tomllib.loads(CONFIG_PATH.read_text())
    return Global(
        list(data.get("clusters", [])),
        list(data.get("workstations", [])),
        dict(data.get("projects", {})),
    )


def save_global(g):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        tomli_w.dumps(
            {
                "clusters": g.clusters,
                "workstations": g.workstations,
                "projects": g.projects,
            }
        )
    )


def require_clusters():
    g = load_global()
    if not g.clusters:
        raise CtError("no clusters configured — run `ct init`")
    return g


# --- projects ------------------------------------------------------------


@dataclass
class Project:
    name: str
    root: Path
    branch: str
    targets: dict  # cluster alias -> repo path on that cluster

    @property
    def state_dir(self):
        d = STATE_DIR / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d


def read_project(root):
    root = Path(root)
    f = root / PROJECT_FILE
    if not f.exists():
        raise CtError(f"{f} not found — run `ct init .` there")
    data = tomllib.loads(f.read_text())
    targets = {a: t["path"] for a, t in data.get("targets", {}).items()}
    return Project(data["name"], root, data.get("branch", "main"), targets)


def write_project(p):
    (p.root / PROJECT_FILE).write_text(
        tomli_w.dumps(
            {
                "name": p.name,
                "branch": p.branch,
                "targets": {a: {"path": path} for a, path in p.targets.items()},
            }
        )
    )


def find_project(name=None):
    """Resolve a project: -p name, else walk up from cwd, else the only one known."""
    g = load_global()
    if name:
        if name not in g.projects:
            known = ", ".join(g.projects) or "none"
            raise CtError(f"unknown project {name!r} — known: {known}")
        return read_project(g.projects[name])
    start = Path.cwd().resolve()
    for d in [start, *start.parents]:
        if (d / PROJECT_FILE).exists():
            return read_project(d)
    if len(g.projects) == 1:
        return read_project(next(iter(g.projects.values())))
    if not g.projects:
        raise CtError("no project here — run `ct init .` inside a repo")
    raise CtError("several projects known; pass -p NAME: " + ", ".join(g.projects))
