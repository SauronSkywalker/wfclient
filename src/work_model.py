# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""WFSim protocol-7 work shapes.

A verifier may hold two *works*:

  - a board PACK: one server lease with one or more `pack.tasks[]` rows
  - a riven gain: one lease with one optimize request

The dispatcher schedules `WorkTask` objects, never the raw HTTP response.  This
keeps the pack lease intact while multiple rows of the pack are computed on
different slots.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

PROTOCOL = 7
CONSENT_V = 3
BOARD_LEASE_SAFE_S = 30.0
RIVEN_LEASE_SAFE_S = 30.0


@dataclass
class WorkTask:
    kind: str                         # "board" | "riven_gain"
    task: dict                         # raw row / riven work
    index: int = 0
    total: int = 1
    state: str = "pending"            # pending | inflight | done | failed
    result: dict | None = None
    compute_ms: int | None = None
    #: the server has accepted this row's answer already (interim upload)
    uploaded: bool = False
    #: THIS ROW'S lease, renewed by a heartbeat.  A pack's lease is no longer a
    #: deadline: a row we are still working on is held for us as long as we
    #: keep renewing, and only a silent client loses one.
    row_until: float = 0.0
    #: when a failed renewal may be retried (a miss is not a refusal)
    renew_at: float = 0.0
    renew_count: int = 0
    #: the server said another machine has this row — stop, do not upload it
    lost: bool = False
    #: one user-selected task outranks board-first and shortest-left until it
    #: starts.  It is runtime-only and is cleared when the task is reserved.
    manual_priority: bool = False
    #: WHEN THIS ROW REACHED US — the only clock that can say how long it has
    #: waited for a free engine.  `row_until` cannot: the heartbeat pushes it
    #: forward forever, so a row nobody ever starts looks forever fresh by it
    #: (`Fleet.pick_for_slot`, the starvation guard).
    claimed_at: float = 0.0

    @property
    def question(self) -> str | None:
        return self.task.get("question")

    @property
    def identity(self) -> str | None:
        return self.task.get("identity")

    def label(self) -> str:
        if self.kind == "riven_gain":
            weapon = self.task.get("weapon") or "?"
            ruler = (self.task.get("ruler") or "").replace("standard_", "")
            return f"riven {weapon}/{ruler}"
        record = self.task.get("record") or {}
        weapon = record.get("weapon") or self.task.get("weapon") or "?"
        ruler = (self.task.get("ruler") or "").replace("standard_", "")
        return f"board {weapon}/{ruler}"


@dataclass
class Work:
    kind: str                         # "board" | "riven_gain"
    lease_id: str
    tasks: list[WorkTask]
    deadline: float
    source: str = "fetch"
    lease_ms: int = 0
    code: str | None = None
    raw: dict = field(default_factory=dict)
    aborted: bool = False
    #: pause is draining: keep the rows already on an engine, start no more
    pausing: bool = False
    renew_count: int = 0
    renew_at: float = 0.0
    #: when this work first reached an engine — the clock the riven's
    #: time-based ahead rule is measured against
    started_at: float = 0.0
    #: only reached when renewals keep FAILING — the safety net that replaces
    #: the old "drop everything at the lease" behaviour
    hard_deadline: float = 0.0
    #: arrived while the riven pool was already full: HELD, never returned for
    #: being extra, and renewed whatever the queue switch.  The flag only
    #: matters while idle — once a slot takes it the inflight rule covers it,
    #: once uploaded it is gone.
    held_extra: bool = False

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.deadline

    def seconds_left(self, now: float | None = None) -> float:
        return self.deadline - (now or time.time())

    def pending_tasks(self) -> list[WorkTask]:
        return [t for t in self.tasks if t.state == "pending" and not t.lost]

    def inflight_tasks(self) -> list[WorkTask]:
        return [t for t in self.tasks if t.state == "inflight" and not t.lost]

    def live_tasks(self) -> list[WorkTask]:
        """Rows we still own and have not finished — the ones to renew."""
        return [t for t in self.tasks
                if not t.lost and t.state in ("pending", "inflight")]

    def done_tasks(self) -> list[WorkTask]:
        return [t for t in self.tasks if t.state == "done"]

    def remaining_count(self) -> int:
        return sum(1 for t in self.tasks if t.state in ("pending", "inflight"))

    def all_done(self) -> bool:
        """Every row is finished OR lost to someone else."""
        return all(t.state == "done" or t.lost for t in self.tasks)

    def is_live(self, now: float | None = None) -> bool:
        """Still ours to work on.

        `deadline` is only the number computed when the lease ARRIVED.  The
        heartbeat moves each ROW's `row_until` and leaves `deadline` behind, so
        anything that means "may we still work on this" has to ask this — not
        `expired()`, which would say no the moment the original lease lapsed
        and would silently strand a pack we had successfully renewed.

        Ownership itself is the server's call: we keep going until the work is
        finished, the rows are lost, or renewals have failed for a very long
        time (`hard_deadline`).
        """
        now = now or time.time()
        if self.aborted or self.all_done():
            return False
        if self.hard_deadline and now >= self.hard_deadline:
            return False
        return True

    def lost_count(self) -> int:
        return sum(1 for t in self.tasks if t.lost)

    def lease_left(self, now: float | None = None) -> float | None:
        """Seconds until the NEAREST live row lapses (None when none live)."""
        now = now or time.time()
        rows = [t.row_until for t in self.live_tasks() if t.row_until]
        return (min(rows) - now) if rows else None

    def next_task(self) -> WorkTask | None:
        for task in self.tasks:
            if task.state == "pending":
                return task
        return None

    def start_task(self, task: WorkTask) -> None:
        if task not in self.tasks or task.state != "pending":
            raise AssertionError("task is not pending in this work")
        task.state = "inflight"

    def record_answer(self, task: WorkTask, result: dict, compute_ms: int) -> None:
        if task not in self.tasks:
            raise AssertionError("task is not in this work")
        task.state = "done"
        task.result = result
        task.compute_ms = compute_ms

    def label(self) -> str:
        if not self.tasks:
            return self.kind
        if self.kind == "riven_gain":
            return self.tasks[0].label()
        first = self.tasks[0]
        weapon = (first.task.get("record") or {}).get("weapon") or "?"
        return f"pack {len(self.tasks)}x {weapon}"


def _lease_deadline(now: float, lease_ms: int, safe_s: float) -> float:
    if lease_ms <= 0:
        lease_ms = 30 * 60 * 1000
    return now + max(30.0, lease_ms / 1000.0 - safe_s)


def work_from_response(work: dict | None, pack: dict | None, now: float | None = None,
                       source: str = "fetch") -> Work | None:
    """Parse `/api/board/work`'s protocol-7 response.

    `work` is the single riven shape; `pack` is the board pack shape.  The
    caller must never infer one from the other.
    """
    now = now or time.time()
    if pack and pack.get("tasks"):
        lease = str(pack.get("lease") or "")
        if not lease:
            return None
        lease_ms = int(pack.get("lease_ms") or 30 * 60 * 1000)
        deadline = _lease_deadline(now, lease_ms, BOARD_LEASE_SAFE_S)
        tasks = [
            WorkTask(kind="board", task=dict(t), index=i,
                     total=len(pack["tasks"]), row_until=deadline,
                     claimed_at=now)
            for i, t in enumerate(pack["tasks"])
        ]
        return Work(kind="board", lease_id=lease, tasks=tasks,
                    deadline=deadline,
                    # The old code turned this deadline into "upload what you
                    # have and throw the rest away".  Now it is the point at
                    # which renewals START, and this is the line that says
                    # "we have been unable to renew for a long time".
                    hard_deadline=now + max(lease_ms / 1000.0, 300.0) * 6.0,
                    source=source, lease_ms=lease_ms, raw=dict(pack))
    if work and work.get("kind") == "riven_gain":
        lease = str(work.get("lease") or "")
        if not lease:
            return None
        lease_ms = 15 * 60 * 1000
        deadline = _lease_deadline(now, lease_ms, RIVEN_LEASE_SAFE_S)
        task = WorkTask(kind="riven_gain", task=dict(work), index=0, total=1,
                        row_until=deadline, claimed_at=now)
        return Work(kind="riven_gain", lease_id=lease, tasks=[task],
                    deadline=deadline,
                    hard_deadline=now + max(lease_ms / 1000.0, 300.0) * 6.0,
                    source=source, lease_ms=lease_ms,
                    code=work.get("code"), raw=dict(work))
    return None


def answer_payload(task: WorkTask, result: dict, compute_ms: int, lanes: int) -> dict:
    if task.kind != "board":
        raise ValueError("answer_payload is for board tasks")
    if not task.question:
        raise ValueError("board task has no question id")
    return {
        "question": task.question,
        "score": result["score"],
        "metric": result["metric"],
        "work": int(result.get("work") or 0),
        "compute_ms": int(compute_ms),
        "lanes": int(lanes),
        # Counts are what the official page sends beside a row.  The server
        # keeps them for statistics and never credits them.
        "counts": dict(result.get("counts") or {}),
    }
