# ct — personal SLURM dispatch CLI

A single-user command-line tool that runs **only on the control-plane machine** (a laptop).
Code lives on a git remote, clusters hold clones, and `ct` orchestrates `git pull` +
`sbatch` + `squeue` over SSH so the user never opens a manual SSH session to dispatch or
watch jobs.

**Simplicity is a hard requirement.** Personal tool, one user, a handful of hosts. Prefer
fewer lines over robustness. No speculative features, no abstractions with a single caller,
no retries, no daemons, no async. When something fails: print the command and its stderr,
keep going where per-target isolation allows, exit non-zero at the end. Target total size:
~800–1200 lines of Python.

---

## 1. Context

- The user authors code on a remote workstation (an ssh alias, e.g. `ws`) through an
  editor, and occasionally edits directly on the laptop. Everything goes through a git
  remote (`origin`).
- Several independent SLURM clusters, **no shared filesystems**, referred to by their
  `~/.ssh/config` aliases — `hpc1`, `hpc2`, `gpu1`, `gpu2` in the examples below. All use
  certificate/key auth with `ForwardAgent yes` (the forwarded agent is what lets clusters
  `git pull` from the remote).
- One plain workstation alias (no SLURM role in `ct` beyond `ct sh`).
- The laptop has a local clone of each project. **The clone is a mirror of `origin`** —
  `ct` answers "what sbatch files exist?" from `origin/<branch>` refs (`git ls-tree`),
  never from the working tree.

**Core rule: clusters only ever run what is on `origin`.** Never local laptop state, never
a cluster-local commit.

## 2. Repo convention

```
demo/
  .ct.toml                 # gitignored — machine-specific paths
  slurm/
    hpc1/  train.sbatch  sweep_lr.sbatch
    hpc2/  train.sbatch
    gpu1/  train_2gpu.sbatch
    gpu2/  ablation.sbatch
  src/
```

- A directory `slurm/<alias>/` whose name matches a **global cluster** (§3.2) makes that
  cluster a **target** for this project. No directory → not a target (this is why the
  workstation alias is never a target).
- Only `*.sbatch` files are offered, so helper scripts (`common.sh`) can sit alongside.
- **CWD convention:** `ct` always submits from the repo root on the cluster
  (`cd <path> && sbatch slurm/<target>/<file>`). SLURM sets the job's working directory to
  the submission directory, so relative paths inside sbatch scripts resolve from the repo
  root, identically on every cluster.

## 3. State

Four small files. All writes are plain rewrites or appends; no locking.

### 3.1 `<repo>/.ct.toml` (per project, gitignored)

```toml
name   = "demo"
branch = "main"

[targets.hpc1]
path = "/home/USER/work/demo"

[targets.gpu1]
path = "/scratch/USER/demo"
```

`path` is the repo location on that cluster, expanded by the **remote** shell (never
`os.path.expanduser` locally). Paths must contain no spaces or shell metacharacters —
validated by `remote.token()` before any interpolation.

### 3.2 `~/.config/ct/config.toml` (global)

Written by the global `ct init` (§5). Hosts are known globally, independent of any project:

```toml
clusters     = ["hpc1", "hpc2", "gpu1", "gpu2"]
workstations = ["ws"]

[projects]
demo = "/home/USER/repos/demo"
```

- `clusters`: SLURM hosts — the default fan-out set for `ct st` / `ct free`, and the
  universe of valid targets everywhere.
- `workstations`: plain SSH hosts, recorded so `ct sh ws` works; never queried for jobs.
- `[projects]`: project name → local clone path.

Project resolution for project-scoped commands: if CWD is inside a repo containing
`.ct.toml` (walk upward), use it; else if `[projects]` has exactly one entry, use that;
else require `-p/--project NAME`.

### 3.3 `~/.local/state/ct/<project>/jobs.jsonl` (append-only)

One line per successfully submitted job:

```json
{"ref": "hpc1:4821", "target": "hpc1", "id": "4821",
 "sbatch": "slurm/hpc1/train_lr3.sbatch", "commit": "a3f1c02",
 "at": "2026-01-01T09:14:02Z"}
```

No stdout path is stored — log paths are resolved at read time (§5 `ct log`).

### 3.4 `~/.local/state/ct/<project>/seen.jsonl` (append-only)

The "already offered" ledger for `ct run all`:

```json
{"target": "hpc1", "path": "slurm/hpc1/train_lr3.sbatch",
 "blob": "a91f3c2...", "status": "submitted", "at": "2026-01-01T09:14:02Z"}
```

- Identity is `(target, path, blob-sha)`. Blob SHAs come free from `git ls-tree`.
  Content change ⇒ new blob ⇒ the file counts as new again ("changed").
- `status` is `"submitted"` or `"skipped"`. **`skipped` ≠ never-seen**: a skipped file is
  not re-offered. Latest line for a given `(target, path)` supplies the "was <blob>" label.
- Nothing is recorded unless its `sbatch` succeeded, or the user explicitly declined it in
  a subset pick (see §5 `ct run all`).

## 4. Job references

Everywhere a command takes a job, accept three forms:

| Form | Example | Resolution |
|---|---|---|
| canonical | `hpc1:4821` | split on `:` — this is what `ct st` and submit output print |
| two-arg | `ct kill gpu1 117` | explicit target + id; works for jobs `ct` never submitted |
| bare id | `4821` | look up in the project's `jobs.jsonl`; error if not found or ambiguous |

The explicit forms resolve against the global `clusters` list and need **no project** —
they work from any directory, for any job. Only the bare form needs a resolvable project.
Job ids are per-cluster counters, so two clusters really can both have `4821`; the bare
form errors out in that case rather than guessing.

Job IDs are **opaque strings**. Array task ids like `4821_3` pass through untouched to
`scontrol`/`scancel`/`squeue` — arrays work without any dedicated code; do not add any.

## 5. Commands

Global option: `-p/--project NAME`. Two scopes:

- **Host-level** — need only the global config, work from any directory: `st`, `free`,
  `kill`, `sh`, `targets`, and `log` with an explicit ref.
- **Project-scoped** — resolve a project per §3.2: `init .`, `run`, `run all`, `new`,
  `push`, `pull`, `sync`, `seen`, plus bare-id refs and `st -a`.

All multi-host operations fan out in parallel (`ThreadPoolExecutor`); an unreachable host
is reported and skipped, and never blocks or fails the others.

### `ct init` — global setup (run once, from anywhere)

Builds the global host roster interactively:

1. Parse `~/.ssh/config` for `Host` aliases. Skip any pattern containing `*`, `?`, or `!`.
   A simple line parser of the top-level file is sufficient (no `Include` recursion).
2. Checkbox: *"Which hosts are SLURM clusters?"* — then a second checkbox over the
   remaining aliases: *"Any plain workstations?"* (either may be left empty).
3. Verify the selected clusters in parallel with `command -v sbatch`, **reporting only
   failures** — unreachable, or no `sbatch` on the default non-interactive PATH (which is
   the condition `ct run` needs, since remote commands are not wrapped in a login shell).
   A clean run prints nothing here. Warnings only; the selection is kept either way, and
   `ct targets` shows full per-host status on demand.
4. Write `~/.config/ct/config.toml`, preserving any existing `[projects]` table.
   Re-running is the edit flow: current entries come pre-checked.

After this, `ct st`, `ct free`, `ct sh`, `ct kill` and explicit-ref `ct log` work with no
project at all.

### `ct init .` — project setup (run inside a git clone)

`ct init PATH` where `PATH` is a repo (typically `.`). Requires the global config — if it's
missing, run the global flow first, then continue.

1. Candidate targets = global `clusters` ∩ directories under `slurm/` in the working tree.
   Unmatched directories are reported and ignored.
2. Probe each candidate over SSH, in parallel: look for an existing clone at `~/<name>`,
   `~/work/<name>`, `/scratch/$USER/<name>` (test for `<p>/.git`). If none found, prompt
   for a path and offer to `git clone <origin-url> <path>` there.
3. Write `.ct.toml` (branch = current local branch), append `.ct.toml` to `.gitignore` if
   absent, add the project under `[projects]` in the global config.
4. **Seed the seen-ledger**: record every sbatch file on `origin/<branch>` as
   `status: "skipped"`, and report the count. (Without this, the first `ct run all` offers
   to launch the entire tree.)

### `ct run [TARGET] [FILES...] [--branch X] [-n] [-y]`

Explicit dispatch to one target.

1. `git fetch --quiet origin` (local).
2. Branch = `--branch` if given, else `.ct.toml` `branch`. If the local checked-out branch
   differs, print one warning line (`on 'lr-exp' locally, dispatching 'main'`) and proceed.
3. Preflight (laptop-authoring support): when HEAD *is* the dispatched branch and the
   branch is ahead of origin or `slurm/` is dirty → prompt `push first? [Y/n]`. Under `-y`
   or `-n`, warn instead and dispatch origin as-is: an unattended flag must not push.
4. Candidates: `git ls-tree -r origin/<branch> -- slurm/<target>/` (gives blob + path).
   If `FILES` given, match by full path or basename, erroring on anything not present.
   Else a multi-select checkbox.
5. Confirm screen: target, `origin/<branch>` short-SHA + subject + relative age, file list.
6. Sync the target (one ssh exec):
   ```
   cd <path> && git fetch --quiet origin
     && (git checkout --quiet <branch> 2>/dev/null || git checkout --quiet -b <branch> --track origin/<branch>)
     && git merge --ff-only --quiet origin/<branch>
     && git rev-parse HEAD
   ```
   The explicit checkout matters: a bare `git pull` would pull whatever branch the cluster
   clone happens to be on. `--ff-only` always — a merge commit created on a cluster
   violates the core rule. **If the printed remote HEAD ≠ local `origin/<branch>`, warn and
   do not submit there.** This check is load-bearing, not belt-and-braces: a stray commit
   on top of origin makes `merge --ff-only` *succeed* as a no-op, so the SHA comparison is
   the only thing that catches it.
7. Submit each file as its own ssh exec (`cd <path> && sbatch --parsable slurm/<t>/<f>`;
   multiplexing makes per-file round trips cheap, and it keeps file↔jobid mapping trivial).
   On success: print `✓ hpc1:4821  train_lr3`, append to `jobs.jsonl` and `seen.jsonl`
   (`submitted`). On failure: print stderr, record nothing, continue with remaining files.
8. Footer: `watch with ct st`. Exit non-zero if anything failed.

### `ct run all [-n] [-y] [-t a,b] [--branch X]`

The sweep path: pull everywhere, offer everything new.

1. Local fetch; compute per-target candidate lists from `origin/<branch>` `ls-tree`.
   If a `slurm/<dir>/` exists on origin but isn't in `.ct.toml`, print a one-line hint
   (`slurm/foo/ has no target — re-run ct init .?`) and ignore it.
2. Sync all reachable targets in parallel (same chain as `ct run` step 6). Print one line
   per target: `hpc1  pulled → a3f1c02  <path>`.
3. Diff against the seen-ledger, considering only targets that synced cleanly: a candidate
   is offered if its `(target, path, blob)` has no ledger entry. Label it `new` (path never
   seen) or `changed (was <old-blob-short>)`.
4. Show the list, then a three-way prompt:
   - *submit all N* → submit everything (per-file, as in `ct run` step 7).
   - *cancel* → abort; **record nothing** ("not now" is not a decision about the files).
   - *select a subset* → multi-select; selected files are submitted, **unselected files are
     recorded as `skipped`** (an explicit pass — they will not be re-offered; resurrect
     with `ct seen --forget`).
5. Flags: `-n` list only, submit nothing, record nothing. `-y` skip the prompt (submit
   all). `-t hpc1,gpu1` restrict targets. Guard: if more than 10 files are offered and `-y`
   was not passed, the prompt's default is *cancel* rather than *submit all* — cheap
   insurance against a rebase or branch switch making a whole tree look new.

### `ct new`

Same detection as `ct run all` steps 1+3 but purely local (fetch + ls-tree + ledger — no
SSH at all). Lists what's pending, submits nothing.

### `ct st [TARGET] [-a] [-w]`

```
TARGET  JOB       NAME       STATE    TIME     NODES/REASON
hpc1    hpc1:101  train_lr3  RUNNING  1:42:11  node042
hpc1    hpc1:102  train_lr4  PENDING  0:00     (Priority)
gpu1    gpu1:117  sweep_bs   RUNNING  0:14:03  gpu11
gpu2    —         (no jobs)
```

- Queries **all global clusters** by default, from any directory, no project needed
  (`squeue` lists all the user's jobs anyway — it was never project-filtered).
  `TARGET` restricts to one cluster. Even inside a project the fan-out set stays global:
  one predictable behavior, no context-dependent scoping.
- Fan out `squeue -u $(whoami) --noheader -o '%i|%j|%T|%M|%R'`. One table, grouped by
  cluster. **Not `--me`** — that flag needs SLURM 20.02+, and a 19.05 controller rejects it
  with a usage error; `-u $(whoami)` works on every version.
- The positional is a **list**, so `all` composes with cluster names rather than occupying
  the slot: `ct st`, `ct st hpc1 gpu1`, `ct st all`, `ct st all hpc1`. Parsing is one line
  (`everyone = "all" in args`), and multi-cluster views fall out for free — `ct free` takes
  the same list.
- `all` shows **every user's** jobs, for judging contention before submitting. Adds a `USER`
  column (`%u`) and a per-cluster count caption, since on a busy cluster the counts are the
  useful part. Refuses to combine with `-a`, which lists only your own finished jobs.
- Forms that hinge on a positional value (`all`, a job reference) cannot appear in the
  command list, so the top-level `--help` carries an epilog spelling them out, and each
  command's docstring lists its own forms.
- `-a`: the one project-scoped flag — for jobs in the project's `jobs.jsonl` absent from
  the live queue of a *reachable* cluster, batch-query per cluster
  `sacct -j id1,id2 -n -P -X --format=JobID,State,ExitCode,Elapsed` and append the rows
  dimmed. If `sacct` fails on a cluster (no accounting DB), show those as
  `finished (state unknown)`. No resolvable project → error with a hint.
- `-w`: wrap in `rich.live.Live`, refresh every 5 s until Ctrl-C.

### `ct free [TARGET]`

Where is there room? All global clusters by default, no project needed. Fan out
`sinfo --noheader -o '%P|%D|%T|%G'` and render one table per cluster: partition, node
count, state, gres. Pure rendering, no interpretation.

### `ct log REF [-f]`

1. Resolve ref → (target, id).
2. `ssh <target> scontrol show job <id>` and parse `StdOut=`, `JobState=`, `Reason=`.
   Asking SLURM beats parsing `#SBATCH --output=` ourselves, which would have to
   reimplement `%j`/`%x`/`%A`/`%a` and cannot resolve `%N` at all.
   - If state is PENDING → print `hpc1:102 is PENDING (Priority) — no output yet`, stop.
   - If `scontrol` no longer knows the job (aged out after completion): fall back to
     `<repo-path>/slurm-<id>.out` (SLURM's default output name in the submission dir —
     which is always the repo root, per the CWD convention). The fallback needs a
     resolvable project for the repo path; without one, or if that file is missing too,
     say what was tried and give up.
3. `ssh <target> tail -n 100 <path>`; with `-f`, `tail -n 20 -f` (Ctrl-C locally kills the
   ssh process — nothing more needed).

Log paths are resolved fresh on every call. Nothing is cached.

### `ct kill REF [REF...]`

Accepts multiple refs. Group by target, one `scancel id1 id2 ...` per target, print what
was cancelled. No confirmation prompt. (`scancel` on an array master ID cancels all tasks
— SLURM's own behavior, no extra code.)

### `ct push [-m MSG]` / `ct pull`

- `push`: commit anything outstanding (`git add -A`, default MSG `ct push`) and
  `git push --set-upstream origin <current branch>`. Clean tree but ahead → just pushes.
  **Re-asserts that `.ct.toml` is ignored before every `git add -A`** (§8.9), and refuses
  outright if it is already tracked, naming `git rm --cached .ct.toml` as the remedy.
- `pull`: `git pull --ff-only origin <current branch>` — explicit rather than relying on
  upstream tracking being configured correctly.

### `ct sync [TARGET|all]`

The sync chain from `ct run` step 6, no submission. Prints each target's resulting HEAD.
Default: all targets. Exit non-zero unless every target reached `origin/<branch>`.

### `ct sh TARGET [-- CMD...]`

- Accepts any host in the global config — clusters **and** workstations.
- If a project resolves and TARGET is one of its targets, prepend `cd <path> && `;
  otherwise plain (landing in `$HOME` is fine).
- No CMD: `ssh -t <target> 'cd <path> && exec $SHELL -l'` — interactive shell.
  (Interactive: **no** BatchMode, add `-t`.)
- With CMD: `ssh <target> 'cd <path> && CMD'`, e.g. `ct sh gpu1 -- nvidia-smi`.

### `ct targets`

Health check, parallel, one row per host. With a resolvable project: the project's targets
— reachable? clone present? current HEAD? `sbatch` found? Without a project: all global
clusters — reachable? `sbatch` found? (no repo columns).

### `ct seen [--forget PATH] [--reset]`

- Bare: render the ledger (target, path, short blob, status, timestamp).
- `--forget PATH`: rewrite the file dropping all entries matching that path (any target)
  — the file becomes "new" again.
- `--reset`: truncate and re-seed from current `origin/<branch>` (all `skipped`), same as
  init's seeding.

## 6. SSH and subprocess layer

One module is the entire remote layer — **shell out to the system `ssh`; never use
paramiko/asyncssh** (the user's `~/.ssh/config` — certs, ForwardAgent, future ProxyJump —
must keep working untouched). Likewise use the `git` binary, not GitPython.

```python
BATCH = ["BatchMode=yes", "ConnectTimeout=5"]
MUX = ["ControlMaster=auto", "ControlPath=~/.ssh/ct-%C", "ControlPersist=10m"]

def run(alias, cmd):
    return subprocess.run([*_argv(), alias, cmd], capture_output=True, text=True)
```

- `BatchMode` fails fast instead of hanging on a prompt; `ConnectTimeout=5` keeps a dead
  host from freezing fan-outs; the ControlMaster options give multiplexing without
  requiring edits to the user's ssh config. `ct sh` uses a variant without BatchMode,
  with `-t`.
- `SSH = os.environ.get("CT_SSH", "ssh")` — setting `CT_SSH=echo` turns every remote call
  into a printed dry run. One line; the whole debugging story.
- Anything interpolated **unquoted** into a remote command (repo paths, branch names, the
  project name) goes through `remote.token()`, which permits `~` and `$USER` — so the
  remote shell still expands them — while rejecting spaces and metacharacters. Filenames
  coming from `ls-tree` are `shlex.quote`d instead.
- `fanout(aliases, cmd_fn)` = `ThreadPoolExecutor(max_workers=8)` over `run`.
- `error(result)` produces the one useful line from a failure: `unreachable` on ssh's 255,
  else the last stderr line.

## 7. Package

```
pyproject.toml            # hatchling backend
src/ct/
  cli.py        # typer app — all commands, orchestration only
  config.py     # global config (clusters/workstations/projects), .ct.toml, state paths
  sshconf.py    # ~/.ssh/config → alias list
  remote.py     # run(), fanout(), stream(), token(), error()
  gitops.py     # local git reads/writes, remote sync + probe command builders
  slurm.py      # squeue/sinfo/sacct/scontrol/sbatch strings + parsers
  ledger.py     # jobs.jsonl + seen.jsonl
  ui.py         # rich tables, questionary prompts
```

```toml
[project]
name = "ct"
requires-python = ">=3.9"
dependencies = ["typer", "rich", "questionary", "tomli-w", "tomli; python_version < '3.11'"]

[project.scripts]
ct = "ct.cli:main"        # main() wraps app() to print CtError as one red line
```

Install: `pip install -e .` **and** `uv tool install --editable .` must both work (same
pyproject serves both). Not published to PyPI.

Style: plain functions and dataclasses; no class hierarchies, no plugins, no config beyond
what §3 lists. A single `CtError(Exception)` for expected failures, caught once in `main()`.
Parsers live next to the command strings they parse.

## 8. Invariants

1. Clusters run what's on `origin` — every submit happens after the sync chain, and only if
   remote HEAD == local `origin/<branch>`.
2. `--ff-only` on every remote merge; explicit `checkout <branch>` before it.
3. Nothing enters `seen.jsonl` as `submitted` unless `sbatch` returned 0.
4. Unreachable target → skipped and reported; never partially recorded, never fatal to
   other targets.
5. `skipped` ≠ never-seen. Declining in a subset pick records `skipped`; cancelling the
   whole batch records nothing.
6. Machine-readable SLURM only: `sbatch --parsable`, `squeue -o`, `sacct -P`, `sinfo -o`.
   Assume nothing newer than SLURM 19.05 — the clusters do not run the same version. Lines
   without a `|` are not data and are dropped, never padded into a row.
7. Remote paths are expanded by the remote shell only.
8. `-y` is genuinely unattended: it answers prompts but never pushes on the user's behalf.
9. `.ct.toml` never reaches a remote. It holds the repo path on each cluster, which
   contains the user's account name, so the ignore rule is re-checked before every
   `git add -A` — not just written once at init. A `reset --hard`, rebase or branch switch
   can delete `.gitignore`, and the next push would otherwise publish the file.
   Note the residual risk this cannot cover: `git add -A` commits *everything* untracked
   in the working tree, so unrelated secrets or data files in a research repo are the
   user's own responsibility.

## 9. Deliberately out of scope — do not implement

No login-shell (`bash -lc`) wrapping of remote commands; no force-push recovery
(`sync --force`); no ssh-agent preflight checks; no `known_hosts` bootstrapping; no cron
mode; no sbatch templating or pass-through sbatch args; no result fetching; no
notifications; no `--include-skipped`; no PyPI packaging; no shell completions; no unit
tests. When a cluster misbehaves in practice, targeted fixes get added then — not before.

## 10. Acceptance walkthrough

```bash
# once ever, from anywhere: pick clusters/workstations out of ~/.ssh/config
ct init

# from any directory, no project needed:
ct st                      # all queues across all clusters
ct free                    # partition/GPU room
ct kill gpu1 117           # explicit refs never need a project

# one-time per project, in the local clone:
ct init .                  # intersects slurm/*/ with global clusters, seeds the ledger

# daily: author elsewhere, push; then here:
ct run all                 # pulls every target, offers what's new
ct st -w                   # live view
ct log hpc1:4821 -f        # follow a log; ct log 4821 works if unambiguous
ct kill hpc1:4821 hpc1:4822
ct st -a                   # + finished jobs via sacct (needs the project)

# authored a fix locally:
ct push -m "fix module load"
ct run all                 # the edited file shows as 'changed', re-offered

# escape hatches
ct sh hpc1                 # shell, already cd'd into the project
ct sh ws                   # workstations work too (no cd)
ct sh gpu1 -- nvidia-smi
ct sync all                # pull everywhere, submit nothing
ct seen --forget slurm/hpc1/train.sbatch
```
