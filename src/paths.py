# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Where the portable package's parts live, seen from inside ``src/``.

The code in this folder is the part that is replaced on an update; ``data/``,
``engine/``, ``engine-src/``, ``toolchain/``, ``build-cache/`` and ``logs/``
stay put.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))          # src/
ROOT = os.path.dirname(HERE)                                # portable root
DATA = os.path.join(ROOT, "data")
LOGS = os.path.join(ROOT, "logs")
ENGINE = os.path.join(ROOT, "engine")
ENGINE_SRC = os.path.join(ROOT, "engine-src")
TOOLCHAIN = os.path.join(ROOT, "toolchain")
BUILD = os.path.join(ROOT, "build-cache")
CONFIG = os.path.join(DATA, "config.json")


def ensure_data() -> None:
    os.makedirs(DATA, exist_ok=True)
