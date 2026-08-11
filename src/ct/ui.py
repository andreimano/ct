"""Terminal output and prompts."""

from __future__ import annotations

import questionary
from rich.console import Console
from rich.table import Table

console = Console()

STATE_COLORS = {
    "RUNNING": "green",
    "PENDING": "yellow",
    "FAILED": "red",
    "TIMEOUT": "red",
    "NODE_FAIL": "red",
    "COMPLETED": "dim",
    "CANCELLED": "dim",
    "IDLE": "green",
    "MIXED": "yellow",
    "ALLOCATED": "dim",
    "DRAIN": "red",
    "DOWN": "red",
}


def table(*columns, shrink=(), shrink_to=32):
    """A borderless table whose rows never wrap.

    One row must stay on one line, or a busy queue is unreadable. `shrink` names the
    free-form columns that absorb truncation; the rest get a floor, because rich otherwise
    shrinks every column in proportion and eats the short, load-bearing ones first.
    """
    if isinstance(shrink, str):
        shrink = (shrink,)
    t = Table(box=None, pad_edge=False, header_style="bold")
    for c in columns:
        flexible = c in shrink
        t.add_column(
            c,
            overflow="ellipsis",
            no_wrap=True,
            max_width=shrink_to if flexible else None,
            # 9 fits the longest SLURM states (CANCELLED, COMPLETED) and any host alias,
            # so those always read in full and truncation lands on the shrink columns.
            min_width=None if flexible else max(len(c), 9),
        )
    return t


def state(value):
    color = STATE_COLORS.get((value or "").upper())
    return f"[{color}]{value}[/]" if color else (value or "")


def ok(msg):
    console.print(f"[green]✓[/] {msg}")


def warn(msg):
    console.print(f"[yellow]![/] {msg}")


def bad(msg):
    console.print(f"[red]✗[/] {msg}")


# questionary returns None on Ctrl-C; turn that into a real interrupt so main() can
# report "aborted" once instead of every caller checking for None.


def _answer(question):
    value = question.ask()
    if value is None:
        raise KeyboardInterrupt
    return value


def pick(message, options, checked=()):
    """Multi-select checkbox. Returns the chosen options (possibly empty)."""
    choices = [questionary.Choice(o, checked=o in checked) for o in options]
    return _answer(questionary.checkbox(message, choices=choices))


def confirm(message, default=True):
    return _answer(questionary.confirm(message, default=default))


def choose(message, options, default=None):
    return _answer(questionary.select(message, choices=options, default=default))


def ask(message, default=""):
    return _answer(questionary.text(message, default=default)).strip()
