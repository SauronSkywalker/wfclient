# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Where the portable package's parts live, seen from inside ``src-<hash>/``.

A package is a SET OF GENERATIONS plus one pointer.  Each of the three big parts
carries the hash of its content in its name, so a new release lands BESIDE the
one in use instead of on top of it:

    python-<h>/   src-<h>/   engine-<h>/      immutable, superseded by a new name
    data/  logs/  engine-local/  engine-src/  this machine's own state, never versioned

``current.txt`` (four lines, written by a single atomic rename) says which
generation is live::

    python-3f9c1a7b2d4e
    src-8a2b6c0d1e3f
    engine-c128e453b2f0
    run-portable-20261022-1330.bat

That is the whole reason the client cannot end up half-updated: a running
python.exe is never overwritten, and the pointer either names the old generation
or the new one -- never a mixture.
"""
from __future__ import annotations

import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))          # src-<h>/
ROOT = os.path.dirname(HERE)                                # portable root
#: The pointer: python generation, src generation, engine generation, launcher.
CURRENT = os.path.join(ROOT, "current.txt")
#: Last generation that started successfully (rollback = copy over current.txt).
PREVIOUS = os.path.join(ROOT, "previous.txt")
DATA = os.path.join(ROOT, "data")
LOGS = os.path.join(ROOT, "logs")
#: This machine's own engine bits: a self-built core and the download line's
#: replacement/rollback copies.  Never inside a generation: an update cannot
#: overwrite it, and it cannot lie about which release it belongs to.
ENGINE_LOCAL = os.path.join(ROOT, "engine-local")
ENGINE_SRC = os.path.join(ROOT, "engine-src")
TOOLCHAIN = os.path.join(ROOT, "toolchain")
BUILD = os.path.join(ROOT, "build-cache")
CONFIG = os.path.join(DATA, "config.json")


def pointer() -> list[str]:
    """The lines of ``current.txt``, or ``[]`` for a pack without one.

    Empty is not an error: a folder that predates the generations layout runs
    from ``python/``, ``src/`` and ``engine/`` exactly as it always did.
    """
    try:
        with open(CURRENT, "r", encoding="utf-8-sig") as f:
            lines = [ln.strip() for ln in f.read().splitlines()]
    except OSError:
        return []
    return lines[:4] if len(lines) >= 3 else []


def current_generation(kind: str) -> str:
    """The live directory name for ``python`` / ``src`` / ``engine`` (or ``""``)."""
    idx = {"python": 0, "src": 1, "engine": 2}.get(kind)
    lines = pointer()
    if not lines or idx is None or idx >= len(lines):
        return ""
    return lines[idx]


def _generation_built_at(folder: str) -> str:
    """The pack time recorded in ``.gen.json`` (``""`` when there is none)."""
    try:
        with open(os.path.join(folder, ".gen.json"), "r", encoding="utf-8") as f:
            stamp = json.load(f).get("built_at")
    except (OSError, ValueError, AttributeError):
        return ""
    return stamp if isinstance(stamp, str) else ""


def _newest_generation(kind: str) -> str:
    """The last ``<kind>-<hash>`` directory, for a pack with no pointer yet.

    A fresh unzip has the generations but has not been installed, so nothing has
    written ``current.txt``; the launcher falls back to the names baked into it
    and the engine has to be found the same way, or the fleet starts and every
    `Popen` answers FileNotFoundError.

    "Newest" is the pack's own ``built_at`` from ``.gen.json`` -- a folder can
    hold two engine generations extracted from two packages, and the hash says
    nothing about which one is later.  A directory without a marker falls back
    to its mtime, so an unpacked-by-hand generation still ranks by age.

    ONLY ``<kind>-<twelve hex digits>`` counts: `engine-src/` and `engine-local/`
    share the `engine-` prefix and sorting them in by name would hand the fleet
    the synced upstream tree as if it were a core.
    """
    pattern = re.compile(rf"^{re.escape(kind)}-[0-9a-f]{{12}}$")
    try:
        names = [n for n in os.listdir(ROOT)
                 if pattern.match(n)
                 and os.path.isdir(os.path.join(ROOT, n))]
    except OSError:
        return ""
    if not names:
        return ""

    def rank(name: str) -> tuple:
        folder = os.path.join(ROOT, name)
        try:
            mtime = os.path.getmtime(folder)
        except OSError:
            mtime = 0.0
        # ONE COMPARABLE STAMP: the pack's own time when the marker is readable,
        # otherwise the folder's, in the same shape -- a generation whose marker
        # is missing must not be ranked *below* one merely because it has none.
        stamp = _generation_built_at(folder) or time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(mtime))
        return (stamp, mtime, name)

    return max(names, key=rank)


def engine_dir() -> str:
    """The live engine generation; ``engine/`` for a pack without either.

    The pointer wins when it names a folder that is really there.  It can name a
    folder that is not: a hand-deleted generation, or a copy that took the three
    names and left one out.  Then -- and whenever there is no pointer at all --
    the newest generation on disk answers, so the fleet starts instead of failing
    every `Popen` with FileNotFoundError.
    """
    name = current_generation("engine")
    if name and os.path.isdir(os.path.join(ROOT, name)):
        return os.path.join(ROOT, name)
    name = _newest_generation("engine")
    return os.path.join(ROOT, name) if name else os.path.join(ROOT, "engine")


def launcher_file() -> str:
    """The launcher this install should start through.

    Line 4 of the pointer when that file exists, else the newest
    ``run-portable-*.bat``, else ``""`` -- a folder with no launcher is started
    straight through the interpreter.  The relaunch after an update uses this:
    the new package's launcher is a NEW FILE, so nothing ever has to overwrite
    a batch file that a console is running.
    """
    lines = pointer()
    if len(lines) > 3 and lines[3]:
        p = os.path.join(ROOT, lines[3])
        if os.path.isfile(p):
            return p
    try:
        bats = [n for n in os.listdir(ROOT)
                if n.startswith("run-portable-") and n.lower().endswith(".bat")]
    except OSError:
        bats = []
    if bats:
        return os.path.join(ROOT, sorted(bats)[-1])
    return ""


#: Resolved once per process: an update swaps the pointer and then relaunches,
#: so a running process never needs to see a different engine generation.
ENGINE = engine_dir()


def ensure_data() -> None:
    os.makedirs(DATA, exist_ok=True)
