# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""What the machine was doing while a row ran.

`engine_phase` says how much CPU a row burned (`cpu_s`) and how many cores that
worked out to (`cores`).  That is the row's own account; it cannot say whether
the difference from a fast row was a heavier row or a row that waited while
something else held the cores.

Two cheap system samples answer that, and only cheap ones are worth running:

* per-core busy percent — one `NtQuerySystemInformation`, 0.08 ms for 16 cpus,
  sampled once a second and kept for fifteen minutes;
* per-thread CPU seconds of the engine — ids cached per process (finding them
  walks every thread on the machine, reading them is 0.02 ms each), so a slow
  row can report whether its lanes were balanced or one thread carried it.

Nothing here changes scheduling, priorities or the request: it only looks.
"""
from __future__ import annotations

import threading
import time

import cores

CORES_KEEP = 900          # 15 minutes at one sample a second
HEALTH_EVERY = 60.0       # one `cpu_health` line a minute
THREAD_CACHE_S = 30.0     # a pool's threads change; not every request
SAMPLE_S = 1.0


def _busy(a, b) -> list[float]:
    """Busy percent per core between two (idle, kernel, user) snapshots."""
    out = []
    for i in range(min(len(a), len(b))):
        idle = b[i][0] - a[i][0]
        kernel = b[i][1] - a[i][1]
        user = b[i][2] - a[i][2]
        total = idle + kernel + user
        out.append(round(100.0 * (total - idle) / total, 1) if total > 0 else 0.0)
    return out


class CpuWatch:
    """One sampler thread for the whole client, not one per engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: list[tuple[float, list[tuple[int, int, int]]]] = []
        self._tids: dict[int, tuple[float, list[int]]] = {}
        self._log = None
        self._thread = None
        self._last_health = 0.0

    # --- producer ------------------------------------------------------------
    def start(self, log=None) -> None:
        if log is not None and self._log is None:
            self._log = log
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="cpu-watch",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(SAMPLE_S)

    def _tick(self) -> None:
        times = cores.system_core_times()
        if not times:
            return
        now = time.time()
        with self._lock:
            self._samples.append((now, times))
            if len(self._samples) > CORES_KEEP:
                del self._samples[:len(self._samples) - CORES_KEEP]
            recent = self._samples[-2:]
        if len(recent) < 2 or now - self._last_health < HEALTH_EVERY:
            return
        busy = _busy(recent[0][1], recent[1][1])
        if not busy or not self._log:
            return
        self._last_health = now
        try:
            self._log.event("cpu_health", cores=busy,
                            busy_avg=round(sum(busy) / len(busy), 1),
                            n_cores=len(busy))
        except Exception:
            pass

    # --- consumers -----------------------------------------------------------
    def mark(self):
        """One exact per-core reading, for bracketing a request.

        The ring samples once a second; a 0.7 s row falls between two ticks and
        would have no window at all.  Two marks cost 0.1 ms together and give
        the row its own cores.
        """
        return cores.system_core_times()

    @staticmethod
    def busy_between(a, b) -> list[float]:
        if not a or not b:
            return []
        return _busy(a, b)

    def thread_snapshot(self, pid: int) -> dict[int, float]:
        """Per-thread CPU seconds of one process, id cache refreshed rarely.

        A pool grows on demand: the first row of a session runs on threads the
        previous look never saw.  When the threads found account for less than
        half the process's CPU the id list is stale by definition, so it is
        re-read then (40 ms), not on a timer.
        """
        if not pid:
            return {}
        now = time.time()
        with self._lock:
            cached = self._tids.get(pid)
        tids = None
        if cached and now - cached[0] < THREAD_CACHE_S:
            tids = cached[1]
        if tids is None:
            tids = cores.thread_ids(pid)
            with self._lock:
                self._tids[pid] = (now, tids)
        got = cores.thread_cpu_seconds(pid, tids)
        total = cores.process_cpu_seconds(pid)
        if total is not None and sum(got.values()) < 0.5 * total:
            tids = cores.thread_ids(pid)      # the pool grew since the last look
            with self._lock:
                self._tids[pid] = (now, tids)
            got = cores.thread_cpu_seconds(pid, tids)
        return got

    def forget(self, pid: int) -> None:
        with self._lock:
            self._tids.pop(pid, None)


WATCH = CpuWatch()


def watch(log=None) -> CpuWatch:
    """The client's one sampler, started on first use."""
    WATCH.start(log)
    return WATCH
