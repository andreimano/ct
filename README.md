# Cluster Tool (ct)

Cluster Tool sends SLURM jobs to more than one cluster. You run it on one computer, for
example a laptop.

## Warning

An AI model (Claude) wrote all of this code. There are no tests. Test each command before
you use it for important work. The `-n` option shows the actions but does not do them.

## How it works

Your computer and each cluster hold a clone of your git repository. `ct` connects with
`ssh`, runs `git pull` on the cluster, then runs `sbatch`.

A cluster runs only the code that is on the git remote. `ct` does not copy files from your
computer. Therefore you must push your work first.

## Install

You need Python 3.9 or later, and the `ssh` and `git` programs.

```bash
pip install -e .
```

Or use `uv tool install --editable .`.

## Set up

Run this command one time:

```bash
ct init
```

Select your SLURM clusters and your workstations from `~/.ssh/config`. `ct` writes them to
`~/.config/ct/config.toml`. Run the command again to change the selection.

Then run this command in a git repository:

```bash
ct init .
```

`ct` finds the clusters for this project. It finds or makes the clone on each cluster. It
writes `.ct.toml` and adds the name to `.gitignore`.

## Directory structure

```
myproject/
  .ct.toml                            # ct makes this file
  slurm/
    hpc1/  train.sbatch  sweep_lr.sbatch
    gpu1/  train_2gpu.sbatch
  src/
```

A directory in `slurm/` must have the `ssh` name of a cluster. Such a directory makes the
cluster a target. `ct` shows only the files that end with `.sbatch`.

`ct` starts each job from the top directory of the repository. Therefore the relative paths
in your sbatch files are correct on every cluster. Put the settings that change from cluster
to cluster in the directory of that cluster, for example the partition and the account.

## Each day

```bash
git push
```

```bash
ct run all
```

`ct run all` runs `git pull` on each cluster, finds the sbatch files that are new, shows
them, and then starts the files that you select.

`ct` keeps a record of the contents of each sbatch file. If you change a file, `ct` shows it
again with the label `changed`. If you select a subset, `ct` marks the other files
`skipped` and does not show them again. Use `ct seen --forget PATH` to show a file again.

## Commands

| Command | Function |
|---|---|
| `ct run all` | Start all of the new sbatch files |
| `ct run hpc1` | Select the files on one cluster |
| `ct new` | Show the new files. Start nothing |
| `ct st` | Show your jobs on all clusters |
| `ct st all` | Show the jobs of all users |
| `ct st -w` | Show your jobs. Refresh every 5 seconds |
| `ct st -a` | Also show your jobs that stopped |
| `ct free` | Show the partitions, the nodes and the GPUs |
| `ct log hpc1:4821` | Show the output of a job. Add `-f` to follow it |
| `ct kill hpc1:4821` | Stop one or more jobs |
| `ct sync` | Run `git pull` on the clusters. Start nothing |
| `ct push` | Commit and push this clone |
| `ct pull` | Update this clone from the git remote |
| `ct sh hpc1` | Open a shell on a host |
| `ct sh hpc1 -- nvidia-smi` | Run one command on a host |
| `ct targets` | Show the status of each host |
| `ct seen` | Show the record of the sbatch files |

The `-y` option answers the questions. `-y` never pushes your work.

## Job names

`ct` shows a job as `hpc1:4821`. Use this name with `ct log` and `ct kill`. You can also
write `ct kill hpc1 4821`, which works for a job that `ct` did not start. You can write only
the number, but `ct` shows an error if two clusters use that number.

## Notes

- `ct` uses the `ssh` program, so your keys, certificates and `ForwardAgent` settings
  continue to work. `ForwardAgent` lets a cluster read your git remote.
- On a cluster, `ct` uses `git merge --ff-only`. `ct` does not start a job if the cluster is
  not at the same commit as the git remote.
- `ct` uses `squeue -u $(whoami)`, which also works with SLURM 19.05.
- Set `CT_SSH=echo` to print the remote commands without running them.
- `.ct.toml` holds the path of the repository on each cluster. Such a path usually contains
  your account name, so `ct` keeps the file out of git. `ct` checks this rule again before
  each `ct push`. If the file is already in git, `ct` stops and tells you what to do.
- `ct push` runs `git add -A`, so it commits every new file in your directory. Check
  `git status` first if you keep data files or secrets there.
