"""SLURM command strings and parsers for their machine-readable output.

Never scrape human-formatted output: every command below pins an explicit format.
"""

from __future__ import annotations

import re
import shlex

from . import remote

# `--me` needs SLURM 20.02+; `-u $(whoami)` works on every version, including 19.05.
SQUEUE = "squeue -u $(whoami) --noheader -o '%i|%j|%T|%M|%R'"
SQUEUE_ALL = "squeue --noheader -o '%i|%u|%j|%T|%M|%R'"
SINFO = "sinfo --noheader -o '%P|%D|%T|%G'"


def submit(path, sbatch_file):
    return f"cd {remote.token(path, 'repo path')} && sbatch --parsable {shlex.quote(sbatch_file)}"


def cancel(ids):
    return "scancel " + " ".join(shlex.quote(i) for i in ids)


def show(job_id):
    return f"scontrol show job {shlex.quote(job_id)}"


def sacct(ids):
    joined = shlex.quote(",".join(ids))
    return f"sacct -j {joined} -n -P -X --format=JobID,State,ExitCode,Elapsed"


def rows(stdout, n):
    """Split pipe-delimited output into rows of exactly n fields.

    Lines without a delimiter are not data — SLURM versions differ in what they print —
    so they are dropped rather than padded into a bogus row.
    """
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line and "|" in line:
            fields = line.split("|")
            out.append((fields + [""] * n)[:n])
    return out


def job_id(stdout):
    """The id from `sbatch --parsable` (which may print 'id;cluster')."""
    lines = [l for l in (stdout or "").splitlines() if l.strip()]
    return lines[-1].strip().split(";")[0] if lines else ""


def field(stdout, key):
    """Pull key=value out of `scontrol show job` output."""
    m = re.search(rf"\b{key}=(\S+)", stdout or "")
    return m.group(1) if m else None
