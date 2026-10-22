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

import os

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


def engine_dir() -> str:
    """The live engine generation; ``engine/`` for a pack without a pointer."""
    name = current_generation("engine")
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
