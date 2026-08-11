"""ct — dispatch and watch SLURM jobs on several clusters from one machine.

The machine running ct is a control plane. Code travels through origin: this clone is a
mirror of it, clusters hold clones of it, and the invariant everywhere is that clusters
only ever run what is on origin.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import List, Optional

import typer

from . import gitops, ledger, remote, slurm, sshconf, ui
from .config import (CONFIG_PATH, CtError, Global, Project, find_project,
                     load_global, require_clusters, save_global, write_project)
from .ui import console

def _forms(*rows):
    """Lay out example commands for --help.

    Separated by blank lines, not newlines: typer before 0.20 reflows help text into a
    paragraph, and a blank line is the only break that every version keeps. Markdown
    fences and click's \\b marker are both swallowed.
    """
    return "\n\n".join(f"{cmd:<24}{what}" for cmd, what in rows)


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Dispatch and watch SLURM jobs on several clusters.",
    # These forms hinge on a positional value ('all', a job reference), so they cannot
    # appear in the command list above. Spell them out here instead.
    epilog=(
        "Forms that use a positional value, so they are not listed above:\n\n"
        + _forms(
            ("ct run all", "submit every sbatch file that is new or changed"),
            ("ct st all [CLUSTER]", "every user's jobs, not only yours"),
            ("ct log hpc1:4821 -f", "follow a job's output"),
            ("ct kill hpc1 4821", "cluster and id as two words, for any job"),
        )
        + "\n\nAdd -n to preview a command. Add -y to skip its questions."
    ),
)

PROJECT = typer.Option(None, "-p", "--project", help="Project name.")

# Offering more than this many experiments at once makes the prompt default to No —
# a rebase or a branch switch can otherwise make a whole tree look new.
BATCH_GUARD = 10


# --- shared helpers ------------------------------------------------------


def _project_or_none(name=None):
    try:
        return find_project(name)
    except CtError:
        return None


def _clusters(g, names):
    """Validate cluster names, keeping config order. No names means every cluster."""
    if not names:
        return g.clusters
    unknown = [n for n in names if n not in g.clusters]
    if unknown:
        raise CtError(
            f"unknown cluster: {', '.join(unknown)} — known: {', '.join(g.clusters)}"
        )
    return [c for c in g.clusters if c in names]


def _branch(p, override):
    """The branch to dispatch, warning if the local checkout differs."""
    branch = override or p.branch
    current = gitops.current_branch(p.root)
    if current != branch:
        ui.warn(f"on '{current}' locally, dispatching '{branch}'")
    return branch, current


def _preflight(p, branch, current, yes=False):
    """Offer to push local work. Only meaningful when HEAD is the dispatched branch."""
    if current != branch:
        return
    if not (gitops.ahead(p.root, branch) or gitops.dirty(p.root, "slurm")):
        return
    if yes:
        # -y must stay non-interactive, and pushing is too big a side effect to do silently.
        ui.warn("local changes are not on origin — dispatching origin as-is")
        return
    if ui.confirm("local changes are not on origin — push first?", default=True):
        gitops.push(p.root, "ct push")
        gitops.fetch(p.root)


def _sync(p, branch, targets, expect):
    """Bring targets to origin/<branch>. Returns {target: head} for the ones that made it."""
    good = {}
    results = remote.fanout(targets, lambda a: gitops.sync_cmd(p.targets[a], branch))
    for t in targets:
        r = results[t]
        if r.returncode != 0:
            ui.bad(f"{t:10} {remote.error(r)}")
            continue
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        head = lines[-1].strip() if lines else ""
        if head != expect:
            ui.warn(
                f"{t:10} HEAD {head[:7] or '?'} != origin/{branch} {expect[:7]}"
                " — not submitting here"
            )
            continue
        console.print(
            f"  {t:10} pulled → [cyan]{head[:7]}[/]  [dim]{p.targets[t]}[/]",
            no_wrap=True,
            overflow="ellipsis",
        )
        good[t] = head
    return good


def _submit(p, target, files, commit):
    """Submit [(blob, path)] to one target. Returns the ones that went out."""
    done = []
    for blob, path in files:
        r = remote.run(target, slurm.submit(p.targets[target], path))
        if r.returncode != 0:
            ui.bad(f"{path}: {remote.error(r)}")
            continue
        job = slurm.job_id(r.stdout)
        ledger.add_job(p, target, job, path, commit[:7])
        ui.ok(f"[cyan]{target}:{job}[/]  {Path(path).stem}")
        done.append((blob, path))
    if done:
        ledger.mark_seen(p, [(target, path, blob) for blob, path in done], "submitted")
    return done


def _pending(p, ref, targets):
    """[(target, blob, path, label)] for sbatch files never offered before."""
    keys, last = ledger.seen_index(p)
    out = []
    for t in targets:
        for blob, path in gitops.sbatch_files(p.root, ref, t):
            if (t, path, blob) in keys:
                continue
            previous = last.get((t, path))
            label = f"changed (was {previous[:7]})" if previous else "new"
            out.append((t, blob, path, label))
    return out


def _resolve_refs(args, project_name):
    """Job references in any of the three accepted forms -> [(target, id)]."""
    g = require_clusters()
    if len(args) == 2 and args[0] in g.clusters and ":" not in args[1]:
        return [(args[0], args[1])]
    lookup = None
    pairs = []
    for a in args:
        if ":" in a:
            target, _, job = a.partition(":")
            if target not in g.clusters:
                raise CtError(f"unknown cluster {target!r} in {a!r}")
            pairs.append((target, job))
            continue
        if lookup is None:
            lookup = {}
            for j in ledger.jobs(find_project(project_name)):
                lookup.setdefault(j["id"], set()).add(j["target"])
        targets = lookup.get(a, set())
        if not targets:
            raise CtError(f"job {a} is not in this project's history — use <cluster>:{a}")
        if len(targets) > 1:
            found = ", ".join(sorted(targets))
            raise CtError(f"job {a} exists on {found} — use <cluster>:{a}")
        pairs.append((next(iter(targets)), a))
    return pairs


# --- init ---------------------------------------------------------------


@app.command()
def init(
    path: Optional[str] = typer.Argument(
        None, help="Repo to set up (usually '.'). Omit for global setup."
    )
):
    """Set up ct globally, or set up a project with `ct init .`."""
    if path is None:
        _init_global()
        return
    if not load_global().clusters:
        _init_global()
        console.print()
    _init_project(Path(path).expanduser().resolve())


def _init_global():
    g = load_global()
    names = sshconf.aliases()
    if not names:
        raise CtError("no Host entries found in ~/.ssh/config")

    clusters = ui.pick("Which hosts are SLURM clusters?", names, checked=g.clusters)
    rest = [n for n in names if n not in clusters]
    workstations = (
        ui.pick("Any plain workstations?", rest, checked=g.workstations) if rest else []
    )

    # Only the absence is worth reporting. A cluster where `command -v sbatch` fails over
    # a non-interactive ssh is one where `ct run` cannot submit, because remote commands
    # are not wrapped in a login shell.
    if clusters:
        console.print()
        results = remote.fanout(clusters, lambda a: "command -v sbatch")
        for c in clusters:
            r = results[c]
            if r.returncode == 255:
                ui.warn(f"{c:10} unreachable — could not check for sbatch")
            elif r.returncode != 0:
                ui.warn(f"{c:10} no sbatch on the default PATH — not a SLURM cluster?")

    save_global(Global(clusters, workstations, g.projects))
    ui.ok(f"wrote {CONFIG_PATH}")


def _init_project(root):
    g = require_clusters()
    if not (root / ".git").exists():
        raise CtError(f"{root} is not a git repository")
    slurm_dir = root / "slurm"
    if not slurm_dir.is_dir():
        raise CtError("no slurm/ directory — create slurm/<cluster>/ first")

    dirs = sorted(d.name for d in slurm_dir.iterdir() if d.is_dir())
    for d in dirs:
        if d not in g.clusters:
            ui.warn(f"slurm/{d}/ is not a configured cluster — ignored")
    candidates = [d for d in dirs if d in g.clusters]
    if not candidates:
        raise CtError(
            "no slurm/<cluster>/ matches a configured cluster "
            f"({', '.join(g.clusters)})"
        )

    name = root.name
    url = gitops.origin_url(root)
    branch = gitops.current_branch(root)
    remote.token(name, "project name")

    targets = {}
    found = remote.fanout(candidates, lambda a: gitops.find_clone_cmd(name))
    for t in candidates:
        existing = (found[t].stdout or "").strip().splitlines()
        if existing:
            targets[t] = existing[0].strip()
            ui.ok(f"{t:10} found {targets[t]}")
            continue
        if found[t].returncode == 255:
            ui.bad(f"{t:10} unreachable — skipped")
            continue
        ui.warn(f"{t:10} no clone found")
        where = remote.token(ui.ask(f"path for {name} on {t}?", f"~/{name}"), "repo path")
        if not ui.confirm(f"clone into {where} on {t}?", default=True):
            continue
        r = remote.run(t, f"git clone {shlex.quote(url)} {where}")
        if r.returncode != 0:
            ui.bad(f"{t:10} clone failed: {remote.error(r)}")
            continue
        targets[t] = where
        ui.ok(f"{t:10} cloned into {where}")

    if not targets:
        raise CtError("no usable targets — nothing written")

    project = Project(name, root, branch, targets)
    write_project(project)
    gitops.ensure_ignored(root)
    g.projects[name] = str(root)
    save_global(g)

    gitops.fetch(root)
    ref = f"origin/{branch}"
    entries = [
        (t, path, blob)
        for t in targets
        for blob, path in gitops.sbatch_files(root, ref, t)
    ]
    ledger.reset(project, entries)

    console.print()
    ui.ok(f"wrote {root / '.ct.toml'} ({', '.join(targets)})")
    ui.ok(
        f"{len(entries)} existing sbatch files marked as seen "
        "— `ct seen --reset` to change"
    )


# --- run ----------------------------------------------------------------


@app.command()
def run(
    target: Optional[str] = typer.Argument(None, help="Cluster name, or 'all'."),
    files: Optional[List[str]] = typer.Argument(None, help="sbatch files to submit."),
    branch: Optional[str] = typer.Option(None, "--branch", help="Override the branch."),
    dry: bool = typer.Option(False, "-n", help="List only; submit nothing."),
    yes: bool = typer.Option(False, "-y", help="Skip the confirmation prompt."),
    only: Optional[str] = typer.Option(
        None, "-t", help="Comma-separated targets ('run all' only)."
    ),
    project_name: Optional[str] = PROJECT,
):
    """Submit sbatch files to a cluster.

    ct run all              every file that is new or changed, on every cluster

    ct run all -t hpc1      the same, restricted to some clusters

    ct run hpc1             pick from one cluster interactively

    ct run hpc1 a.sbatch    submit named files
    """
    p = find_project(project_name)
    if target == "all":
        if files:
            raise CtError("`ct run all` takes no file names — it submits what is new")
        _run_all(p, branch, only, dry, yes)
        return
    if only:
        raise CtError("-t only applies to `ct run all`")
    if target is None:
        if len(p.targets) != 1:
            raise CtError(
                f"which target? {', '.join(p.targets)} — or `ct run all`"
            )
        target = next(iter(p.targets))
    _run_one(p, target, files or [], branch, dry, yes)


def _run_one(p, target, names, branch_opt, dry, yes):
    if target not in p.targets:
        raise CtError(f"{target!r} is not a target of {p.name} ({', '.join(p.targets)})")
    br, current = _branch(p, branch_opt)
    gitops.fetch(p.root)
    _preflight(p, br, current, yes or dry)

    ref = f"origin/{br}"
    head = gitops.sha(p.root, ref)
    candidates = gitops.sbatch_files(p.root, ref, target)
    if not candidates:
        raise CtError(f"no *{gitops.SBATCH_EXT} under slurm/{target}/ on {ref}")

    if names:
        wanted = set(names)
        chosen = [
            (b, path)
            for b, path in candidates
            if path in wanted or Path(path).name in wanted
        ]
        missing = wanted - {path for _, path in chosen} - {
            Path(path).name for _, path in chosen
        }
        if missing:
            raise CtError(f"not on {ref} under slurm/{target}/: {', '.join(sorted(missing))}")
    elif dry:
        chosen = candidates  # -n means "show me": listing beats prompting for a choice
    else:
        picked = set(ui.pick(f"sbatch files for {target}", [path for _, path in candidates]))
        chosen = [(b, path) for b, path in candidates if path in picked]

    if not chosen:
        console.print("[dim]nothing selected[/]")
        return

    console.print(f"\n{target}  [cyan]{gitops.describe(p.root, ref)}[/]")
    for _, path in chosen:
        console.print(f"  {path}")
    if dry:
        console.print("\n[dim]-n: nothing submitted[/]")
        return
    if not yes and not ui.confirm(f"submit {len(chosen)} to {target}?", default=True):
        return

    console.print()
    if not _sync(p, br, [target], head):
        raise SystemExit(1)
    console.print()
    if len(_submit(p, target, chosen, head)) != len(chosen):
        raise SystemExit(1)
    console.print("\nwatch with [bold]ct st[/]")


def _run_all(p, branch_opt, only, dry, yes):
    br, current = _branch(p, branch_opt)
    gitops.fetch(p.root)
    _preflight(p, br, current, yes or dry)

    ref = f"origin/{br}"
    head = gitops.sha(p.root, ref)
    wanted = [t.strip() for t in only.split(",")] if only else None
    if wanted:
        unknown = [t for t in wanted if t not in p.targets]
        if unknown:
            raise CtError(f"not targets of {p.name}: {', '.join(unknown)}")
    targets = [t for t in p.targets if not wanted or t in wanted]

    for d in gitops.slurm_dirs(p.root, ref):
        if d not in p.targets:
            ui.warn(f"slurm/{d}/ has no target — re-run `ct init .`?")

    ui.ok(f"fetch origin — [cyan]{gitops.describe(p.root, ref)}[/]")
    console.print()

    failed = False
    if dry:
        reached = list(targets)
    else:
        reached = list(_sync(p, br, targets, head))
        failed = len(reached) != len(targets)
        console.print()

    pending = _pending(p, ref, reached)
    if not pending:
        console.print("[dim]nothing new[/]")
        return

    console.print(f"{len(pending)} new experiment{'s' if len(pending) > 1 else ''}:\n")
    for t, _, path, label in pending:
        console.print(f"  {t:10} {path:44} [dim]{label}[/]")
    console.print()
    if dry:
        console.print("[dim]-n: nothing submitted[/]")
        return

    keep, drop = pending, []
    if not yes:
        options = [f"submit all {len(pending)}", "select a subset", "cancel"]
        default = options[2] if len(pending) > BATCH_GUARD else options[0]
        answer = ui.choose("What now?", options, default=default)
        if answer == options[2]:
            return
        if answer == options[1]:
            labels = [f"{t}  {path}" for t, _, path, _ in pending]
            picked = set(ui.pick("select experiments", labels))
            keep = [x for x, l in zip(pending, labels) if l in picked]
            drop = [x for x, l in zip(pending, labels) if l not in picked]
        console.print()

    submitted, wanted = 0, 0
    for t in reached:
        group = [(blob, path) for tt, blob, path, _ in keep if tt == t]
        if group:
            wanted += len(group)
            submitted += len(_submit(p, t, group, head))
    if drop:
        ledger.mark_seen(p, [(t, path, blob) for t, blob, path, _ in drop], "skipped")
        console.print(f"[dim]{len(drop)} marked skipped[/]")
    console.print(f"\n{submitted} submitted · watch with [bold]ct st[/]")
    if failed or submitted != wanted:
        raise SystemExit(1)


@app.command()
def new(
    branch: Optional[str] = typer.Option(None, "--branch"),
    project_name: Optional[str] = PROJECT,
):
    """List sbatch files ct has never offered. Local only — no SSH."""
    p = find_project(project_name)
    br, _ = _branch(p, branch)
    gitops.fetch(p.root)
    ref = f"origin/{br}"
    pending = _pending(p, ref, list(p.targets))
    if not pending:
        console.print("[dim]nothing new[/]")
        return
    t = ui.table("TARGET", "SBATCH", "")
    for target, _, path, label in pending:
        t.add_row(target, path, f"[dim]{label}[/]")
    console.print(t)


# --- watching -----------------------------------------------------------


def _queue_table(clusters, project, everyone=False):
    columns = ["TARGET", "JOB", "NAME", "STATE", "TIME", "NODES/REASON"]
    if everyone:
        columns.insert(2, "USER")
    t = ui.table(*columns, shrink=("NAME", "NODES/REASON"), shrink_to=30)
    filler = [""] * (len(columns) - 3)
    live, counts = {}, {}
    results = remote.fanout(
        clusters, lambda a: slurm.SQUEUE_ALL if everyone else slurm.SQUEUE
    )
    for c in clusters:
        r = results[c]
        if r.returncode != 0:
            t.add_row(c, "—", f"[red]{remote.error(r)}[/]", *filler)
            continue
        rows = slurm.rows(r.stdout, len(columns) - 1)
        counts[c] = len(rows)
        if not everyone:
            live[c] = {row[0] for row in rows}
        if not rows:
            t.add_row(c, "—", "[dim](no jobs)[/]", *filler)
            continue
        for row in rows:
            job, rest = row[0], row[1:]
            *before, state, elapsed, reason = rest
            t.add_row(c, f"{c}:{job}", *before, ui.state(state), elapsed, reason)
    if everyone:
        # With every user's jobs the row count is the useful part, so summarise it.
        per = " · ".join(f"{c} {counts[c]}" for c in clusters if c in counts)
        t.caption = f"{per} · {sum(counts.values())} queued in total"
    elif project:
        _add_finished(t, project, live)
    return t


def _add_finished(t, project, live):
    """Append registry jobs that are no longer in the queue, via sacct."""
    by_target = {}
    for j in ledger.jobs(project):
        target = j["target"]
        if target in live and j["id"] not in live[target]:
            by_target.setdefault(target, {})[j["id"]] = j
    if not by_target:
        return
    results = remote.fanout(by_target, lambda a: slurm.sacct(list(by_target[a])))
    for target, jobs in by_target.items():
        r = results[target]
        rows = slurm.rows(r.stdout, 4) if r.returncode == 0 else []
        reported = set()
        for job, state, code, elapsed in rows:
            base = job.split("_")[0].split(".")[0]
            reported.update({job, base})
            entry = jobs.get(job) or jobs.get(base)
            name = Path(entry["sbatch"]).stem if entry else job
            t.add_row(
                f"[dim]{target}[/]", f"[dim]{target}:{job}[/]", f"[dim]{name}[/]",
                f"[dim]{state}[/]", f"[dim]{elapsed}[/]", f"[dim]exit {code}[/]",
            )
        for job, entry in jobs.items():
            if job not in reported:
                t.add_row(
                    f"[dim]{target}[/]", f"[dim]{target}:{job}[/]",
                    f"[dim]{Path(entry['sbatch']).stem}[/]", "[dim]finished[/]", "",
                    "[dim](state unknown)[/]",
                )


@app.command()
def st(
    targets: Optional[List[str]] = typer.Argument(
        None,
        metavar="[all] [CLUSTER...]",
        help="Say 'all' for every user's jobs. Name clusters to limit the view.",
    ),
    show_all: bool = typer.Option(
        False, "-a", "--all", help="Also show finished jobs from this project."
    ),
    watch: bool = typer.Option(False, "-w", "--watch", help="Refresh every 5s."),
    project_name: Optional[str] = PROJECT,
):
    """Show the SLURM queue. By default, your own jobs on every cluster.

    ct st all               every user's jobs, not only yours

    ct st all hpc1          the same, for one cluster

    ct st hpc1 gpu1         your jobs on some of the clusters
    """
    g = require_clusters()
    args = list(targets or [])
    everyone = "all" in args
    if everyone and show_all:
        raise CtError("-a lists your own finished jobs; it does not combine with `st all`")
    clusters = _clusters(g, [a for a in args if a != "all"])
    project = find_project(project_name) if show_all else None

    def render():
        return _queue_table(clusters, project, everyone)

    if not watch:
        console.print(render())
        return
    from rich.live import Live

    with Live(render(), console=console, screen=False) as live:
        while True:
            time.sleep(5)
            live.update(render())


@app.command()
def free(
    targets: Optional[List[str]] = typer.Argument(
        None, metavar="[CLUSTER...]", help="Limit to these clusters."
    )
):
    """Show partitions, nodes and GPUs across all clusters."""
    clusters = _clusters(require_clusters(), list(targets or []))
    results = remote.fanout(clusters, lambda a: slurm.SINFO)
    for c in clusters:
        r = results[c]
        console.print(f"\n[bold]{c}[/]")
        if r.returncode != 0:
            console.print(f"  [red]{remote.error(r)}[/]")
            continue
        rows = slurm.rows(r.stdout, 4)
        if not rows:
            console.print("  [dim](no partitions reported)[/]")
            continue
        t = ui.table("PARTITION", "NODES", "STATE", "GRES")
        for partition, nodes, state, gres in rows:
            t.add_row(partition, nodes, ui.state(state), "—" if gres in ("", "(null)") else gres)
        console.print(t)


@app.command()
def log(
    ref: List[str] = typer.Argument(..., help="cluster:id, 'cluster id', or a bare id."),
    follow: bool = typer.Option(False, "-f", "--follow", help="Tail the file."),
    project_name: Optional[str] = PROJECT,
):
    """Print a job's output, resolving the path from SLURM."""
    target, job = _resolve_refs(list(ref), project_name)[0]

    path, quoted = None, True
    r = remote.run(target, slurm.show(job))
    if r.returncode == 0:
        state = (slurm.field(r.stdout, "JobState") or "").upper()
        if state == "PENDING":
            reason = slurm.field(r.stdout, "Reason") or "?"
            console.print(f"{target}:{job} is PENDING ({reason}) — no output yet")
            return
        path = slurm.field(r.stdout, "StdOut")
    if not path:
        # scontrol forgets finished jobs. Fall back to SLURM's default output name in
        # the submission directory, which is always the repo root (see README).
        p = _project_or_none(project_name)
        if p and target in p.targets:
            path, quoted = f"{p.targets[target]}/slurm-{job}.out", False
    if not path:
        raise CtError(
            f"no output path for {target}:{job} — SLURM no longer knows the job and "
            "there is no project here to guess the repo path from"
        )

    where = shlex.quote(path) if quoted else remote.token(path, "output path")
    tail = f"tail -n {'20 -f' if follow else '100'} {where}"
    if follow:
        raise SystemExit(remote.stream(target, tail))
    r = remote.run(target, tail)
    if r.returncode != 0:
        raise CtError(remote.error(r))
    print(r.stdout, end="")


@app.command()
def kill(
    refs: List[str] = typer.Argument(..., help="One or more job references."),
    project_name: Optional[str] = PROJECT,
):
    """Cancel jobs."""
    by_target = {}
    for target, job in _resolve_refs(list(refs), project_name):
        by_target.setdefault(target, []).append(job)
    failed = False
    for target, jobs in by_target.items():
        r = remote.run(target, slurm.cancel(jobs))
        if r.returncode != 0:
            ui.bad(f"{target}: {remote.error(r)}")
            failed = True
        else:
            ui.ok(f"cancelled on {target}: {' '.join(jobs)}")
    if failed:
        raise SystemExit(1)


# --- repo plumbing ------------------------------------------------------


@app.command()
def push(
    message: str = typer.Option("ct push", "-m", "--message"),
    project_name: Optional[str] = PROJECT,
):
    """Commit and push this clone."""
    p = find_project(project_name)
    ui.ok(f"pushed {gitops.push(p.root, message)}")


@app.command()
def pull(project_name: Optional[str] = PROJECT):
    """Fast-forward this clone from origin."""
    p = find_project(project_name)
    gitops.pull(p.root)
    ui.ok(gitops.describe(p.root, "HEAD"))


@app.command()
def sync(
    target: Optional[str] = typer.Argument(None, help="Cluster name, or 'all'."),
    branch: Optional[str] = typer.Option(None, "--branch"),
    project_name: Optional[str] = PROJECT,
):
    """Pull on the clusters without submitting anything."""
    p = find_project(project_name)
    br, _ = _branch(p, branch)
    gitops.fetch(p.root)
    head = gitops.sha(p.root, f"origin/{br}")
    targets = list(p.targets) if target in (None, "all") else [target]
    for t in targets:
        if t not in p.targets:
            raise CtError(f"{t!r} is not a target of {p.name} ({', '.join(p.targets)})")
    if len(_sync(p, br, targets, head)) != len(targets):
        raise SystemExit(1)


@app.command()
def sh(
    target: str = typer.Argument(..., help="Any configured host."),
    cmd: Optional[List[str]] = typer.Argument(None, help="Command after `--`."),
    project_name: Optional[str] = PROJECT,
):
    """Open a shell on a host, or run one command there."""
    g = load_global()
    if target not in g.hosts:
        raise CtError(f"unknown host {target!r} — known: {', '.join(g.hosts) or 'none'}")
    p = _project_or_none(project_name)
    prefix = ""
    if p and target in p.targets:
        prefix = f"cd {remote.token(p.targets[target], 'repo path')} && "
    if cmd:
        raise SystemExit(remote.stream(target, prefix + " ".join(cmd)))
    raise SystemExit(remote.stream(target, prefix + "exec $SHELL -l", tty=True))


@app.command()
def targets(project_name: Optional[str] = PROJECT):
    """Check that hosts are reachable and set up."""
    p = _project_or_none(project_name)
    if not p:
        g = require_clusters()
        t = ui.table("CLUSTER", "REACHABLE", "SBATCH")
        results = remote.fanout(g.clusters, lambda a: "command -v sbatch")
        for c in g.clusters:
            r = results[c]
            reachable = r.returncode != 255
            t.add_row(
                c,
                "[green]yes[/]" if reachable else "[red]no[/]",
                "[green]yes[/]" if r.returncode == 0 else "[red]no[/]",
            )
        console.print(t)
        console.print("\n[dim]no project here — showing all clusters[/]")
        return

    t = ui.table("TARGET", "PATH", "HEAD", "SBATCH", shrink="PATH", shrink_to=44)
    results = remote.fanout(p.targets, lambda a: gitops.probe_cmd(p.targets[a]))
    for target, path in p.targets.items():
        r = results[target]
        if r.returncode == 255:
            t.add_row(target, path, "[red]unreachable[/]", "")
            continue
        fields = dict(
            line.split("=", 1)
            for line in r.stdout.splitlines()
            if "=" in line
        )
        head = fields.get("head") or "?"
        t.add_row(
            target,
            path,
            "[red]not cloned[/]" if head == "missing" else head,
            "[green]yes[/]" if fields.get("sbatch") == "yes" else "[red]no[/]",
        )
    console.print(t)


@app.command()
def seen(
    forget: Optional[str] = typer.Option(
        None, "--forget", metavar="PATH", help="Make one sbatch file new again."
    ),
    reset: bool = typer.Option(False, "--reset", help="Re-baseline everything as seen."),
    project_name: Optional[str] = PROJECT,
):
    """Inspect or edit the ledger of sbatch files ct has offered."""
    p = find_project(project_name)
    if forget:
        n = ledger.forget(p, forget)
        ui.ok(f"forgot {n} entr{'y' if n == 1 else 'ies'} for {forget}")
        return
    if reset:
        gitops.fetch(p.root)
        ref = f"origin/{p.branch}"
        entries = [
            (t, path, blob)
            for t in p.targets
            for blob, path in gitops.sbatch_files(p.root, ref, t)
        ]
        ledger.reset(p, entries)
        ui.ok(f"re-baselined {len(entries)} sbatch files as seen")
        return
    records = ledger.seen(p)
    if not records:
        console.print("[dim]ledger is empty[/]")
        return
    t = ui.table("TARGET", "SBATCH", "BLOB", "STATUS", "WHEN", shrink="SBATCH", shrink_to=44)
    for r in records:
        t.add_row(r["target"], r["path"], r["blob"][:7], r["status"], r["at"])
    console.print(t)


def main():
    try:
        app()
    except CtError as e:
        console.print(f"[red]error:[/] {e}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]aborted[/]")
        raise SystemExit(130)
