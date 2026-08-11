"""jobs.jsonl and seen.jsonl — one JSON object per line, appended."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _append(path, records):
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps({**r, "at": _now()}) + "\n")


def _overwrite(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


# --- jobs ---------------------------------------------------------------


def jobs(project):
    return _read(project.state_dir / "jobs.jsonl")


def add_job(project, target, job_id, sbatch, commit):
    _append(
        project.state_dir / "jobs.jsonl",
        [
            {
                "ref": f"{target}:{job_id}",
                "target": target,
                "id": job_id,
                "sbatch": sbatch,
                "commit": commit,
            }
        ],
    )


# --- seen ---------------------------------------------------------------
#
# Identity is (target, path, blob): editing an sbatch file gives it a new blob, so it
# counts as new again. "skipped" is a decision, not an absence — a skipped file is not
# re-offered; only `ct seen --forget` brings it back.


def seen(project):
    return _read(project.state_dir / "seen.jsonl")


def mark_seen(project, entries, status):
    """entries: iterable of (target, path, blob)."""
    _append(
        project.state_dir / "seen.jsonl",
        [
            {"target": t, "path": p, "blob": b, "status": status}
            for t, p, b in entries
        ],
    )


def seen_index(project):
    """(set of (target, path, blob) seen, {(target, path): most recent blob})."""
    keys, last = set(), {}
    for r in seen(project):
        keys.add((r["target"], r["path"], r["blob"]))
        last[(r["target"], r["path"])] = r["blob"]
    return keys, last


def forget(project, path):
    """Drop every entry for path (any target). Returns how many were removed."""
    f = project.state_dir / "seen.jsonl"
    records = _read(f)
    kept = [r for r in records if r["path"] != path]
    _overwrite(f, kept)
    return len(records) - len(kept)


def reset(project, entries):
    """Replace the ledger with entries, all marked skipped."""
    _overwrite(project.state_dir / "seen.jsonl", [])
    mark_seen(project, entries, "skipped")
