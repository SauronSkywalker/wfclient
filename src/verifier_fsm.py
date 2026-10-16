# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Per-verifier state machine for the WFSim protocol-7 compute client.

A verifier holds at most two `Work` objects: the current work and one work
fetched with `ahead:true`.  A board work is a pack of rows; a riven work has one
task.  The dispatcher takes `WorkTask` objects out of the current work and puts
them on compute slots.  The pack lease remains the verifier's until every row it
still owns has been answered, or the whole pack is given back.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from work_model import Work, WorkTask


class State(str, Enum):
    IDLE = "idle"              # holds nothing — the ONLY state that may fetch
    FETCHING = "fetching"      # an /api/board/work call is in flight
    HOLDING = "holding"        # holds work with rows waiting for a core
    COMPUTING = "computing"    # at least one row of the current work is running
    UPLOADED = "uploaded"      # transitional compatibility state
    COOLING = "cooling"        # banned / stale — will not fetch until `until`


@dataclass
class Verifier:
    vid: str
    label: str = ""
    state: State = State.IDLE
    lease: Work | None = None
    queued: Work | None = None
    fetching_ahead: bool = False
    account_claimed: bool = False
    claim_pending: bool = False
    banned_until: float = 0.0
    #: board-only mode handed a riven back (`release:true`); the server will not
    #: hand out anything before its own `sync_floor_ms` cooling, so this verifier
    #: asks for work again no earlier than then (`RIVEN_REFUSE_RETRY_S`).
    riven_cooldown_until: float = 0.0
    #: SPARE VERIFIER: one of the ids beyond `active_verifiers`, woken the
    #: moment the residents cannot cover every compute slot and stood down
    #: again once they can.  A spare ignores `queue_fallback`'s board-only
    #: rule — its whole point is to take whatever the pool has, rivens
    #: included, so the engines never starve.
    spare: bool = False
    #: set when the residents have stably covered the slots: the spare stops
    #: asking, gives back its queued pack and finishes what is on its engine.
    retire_at: float = 0.0
    #: consecutive empty answers, for the ask pace (`Fleet._empty_ask_wait`)
    backoff_s: float = 0.0
    #: consecutive empty answers, for the ask pace (`Fleet._empty_ask_wait`)
    empty_asks: int = 0
    last_error: str = ""
    fetched: int = 0
    uploaded: int = 0
    given_back: int = 0
    confirmed_points: int = 0
    #: True once an authoritative per-device points read has landed for this
    #: run (the account device list for a claimed device, or /api/board/points
    #: for an unclaimed one).  Until then `confirmed_points` is not a baseline.
    points_known: bool = False
    #: `confirmed_points` on the first refresh of this run.  Everything above
    #: that line was earned before the client started, so the UI can show the
    #: run's own figures instead of the account's lifetime totals.
    base_confirmed: int | None = None
    tasks_ahead: int = 1
    trail: list = field(default_factory=list)

    def _to(self, state: "State", why: str) -> None:
        self.trail.append((round(time.time(), 3), self.state.value, state.value, why))
        self.state = state

    def history(self, n: int = 12) -> str:
        return " | ".join(f"{s}->{d}({w})" for _, s, d, w in self.trail[-n:])

    # ------------------------------------------------------------- roles
    def works(self) -> list:
        """Every work this verifier holds, with its role.

        BOTH are computable now: the `ahead` work is not a waiting room any
        more.  The dispatcher may put either on a slot, so every place that
        used to say `self.lease` has to ask `work_role`.
        """
        out = []
        if self.lease is not None:
            out.append((self.lease, "primary"))
        if self.queued is not None:
            out.append((self.queued, "ahead"))
        return out

    def work_role(self, work) -> str | None:
        """`"primary"`, `"ahead"`, or None when this verifier does not hold it."""
        if work is None:
            return None
        if self.lease is work:
            return "primary"
        if self.queued is work:
            return "ahead"
        return None

    def sync_state(self, why: str = "sync") -> None:
        """Recompute HOLDING/COMPUTING/IDLE from the works actually held."""
        if self.state in (State.FETCHING, State.COOLING):
            return
        works = [w for w, _ in self.works() if not w.aborted]
        if any(w.inflight_tasks() for w in works):
            state = State.COMPUTING
        elif any(w.pending_tasks() for w in works):
            state = State.HOLDING
        elif self.lease is not None or self.queued is not None:
            state = State.HOLDING
        else:
            state = State.IDLE
        if state is not self.state:
            self._to(state, why)

    # ---------------------------------------------------------------- guard
    def may_fetch(self, now: float | None = None) -> bool:
        now = now or time.time()
        if self.state is State.COOLING and now >= self.banned_until:
            self._to(State.IDLE, "cool-off lapsed")
        return (self.state is State.IDLE and not self.fetching_ahead
                and self.queued is None and now >= self.banned_until)

    # ---------------------------------------------------------- transitions
    def begin_fetch(self, now: float | None = None) -> None:
        if not self.may_fetch(now):
            held = ""
            if self.lease is not None:
                held = f" (holds {self.lease.kind} {self.lease.lease_id})"
            raise AssertionError(
                f"{self.vid}: fetch attempted while {self.state.value}{held}")
        self._to(State.FETCHING, "begin_fetch")

    def got_work(self, work: Work, now: float | None = None) -> Work:
        if self.state is not State.FETCHING:
            raise AssertionError(f"{self.vid}: got work while {self.state.value}")
        self.lease = work
        self.tasks_ahead = max(1, self.tasks_ahead)
        self._to(State.HOLDING, f"got_work:{work.kind}:{len(work.tasks)}")
        self.fetched += len(work.tasks)
        return work

    def can_fetch_ahead(self, now: float | None = None) -> bool:
        return (self.lease is not None and self.queued is None
                and not self.fetching_ahead
                and self.state in (State.HOLDING, State.COMPUTING)
                # NOT `expired()`: the heartbeat moves the rows, not the lease
                # deadline, so that would stop prefetching on a pack we still
                # hold perfectly well.
                and self.lease.is_live(now)
                and (now or time.time()) >= self.banned_until)

    def begin_fetch_ahead(self) -> None:
        if not self.can_fetch_ahead():
            raise AssertionError(
                f"{self.vid}: ahead fetch attempted while {self.state.value}")
        self.fetching_ahead = True

    def got_work_ahead(self, work: Work, now: float | None = None) -> Work | None:
        """Take an ahead fetch's work, or hand the decision back as None.

        TWO DOORS deliver work to one verifier: the protocol-8 sync reply's
        pack and the classic `/work` ahead fetch.  They race — the sync pack
        can fill the ahead slot while the ahead fetch is still on the wire —
        and raising here killed the fetch coroutine and ORPHANED the pack it
        had just been handed (never stored, never released: the server kept
        the lease until it lapsed, and it counted against `packs_held`).
        Returning None lets the caller release the pack instead.  The state
        machine itself is never left inconsistent.
        """
        if not self.fetching_ahead:
            raise AssertionError(f"{self.vid}: ahead work while not fetching ahead")
        self.fetching_ahead = False
        if self.lease is None:
            self.lease = work
        else:
            # An all_done primary stays until its upload clears it; the ahead
            # work waits in the second slot instead of replacing it.
            if self.queued is not None:
                return None            # caller releases `work`; slot is taken
            self.queued = work
        self.sync_state("got_work_ahead")
        self.fetched += len(work.tasks)
        return work

    def got_nothing_ahead(self, note: str = "") -> None:
        if self.fetching_ahead:
            self.fetching_ahead = False
            self.last_error = note

    def cancel_fetch(self, note: str = "") -> None:
        if self.state is State.FETCHING:
            self.last_error = note
            self._to(State.IDLE, f"cancel_fetch:{note}")
        self.fetching_ahead = False

    def give_back_queued(self, note: str = "") -> None:
        if self.queued is None:
            return
        self.queued.aborted = True
        self.given_back += len(self.queued.tasks)
        self.queued = None
        self.last_error = note
        self.sync_state(f"give_back_queued:{note}")

    def got_nothing(self, banned_until: float = 0.0, note: str = "",
                   now: float | None = None) -> None:
        if self.state is not State.FETCHING:
            raise AssertionError(f"{self.vid}: got_nothing while {self.state.value}")
        now = now or time.time()
        self.banned_until = banned_until or 0.0
        self.last_error = note
        self._to(State.COOLING if self.banned_until > now else State.IDLE,
                 f"got_nothing:{note or 'empty'}")

    # -------------------------------------------------------------- compute
    def available_tasks(self) -> list[WorkTask]:
        out: list[WorkTask] = []
        for work, _ in self.works():
            if not work.aborted:
                out.extend(work.pending_tasks())
        return out

    def start_task(self, work: Work, task: WorkTask) -> WorkTask:
        role = self.work_role(work)
        if role is None:
            raise AssertionError(
                f"{self.vid}: start_task on a work it does not hold"
                f"  |  {self.history()}")
        if self.state not in (State.HOLDING, State.COMPUTING):
            # A lease loaded from config/state can be HOLDING-by-content while
            # the enum still says IDLE; trust the works, not the enum.
            self.sync_state("start_task")
        if self.state not in (State.HOLDING, State.COMPUTING):
            raise AssertionError(
                f"{self.vid}: start_task({role}) while {self.state.value}"
                f"  |  {self.history()}")
        work.start_task(task)
        self._to(State.COMPUTING, f"start_task:{role}")
        return task

    def record_task_answer(self, work: Work, task: WorkTask,
                           result: dict, compute_ms: int) -> None:
        """Record a completed row locally; the pack is uploaded as one batch."""
        if self.work_role(work) is None:
            # The pack was given back/replaced while this answer was in flight;
            # the server will drop the whole batch, so do not touch the new work.
            return
        work.record_answer(task, result, compute_ms)

    def mark_uploaded(self, work: Work, tasks: list[WorkTask]) -> int:
        """Account only after the server has accepted the batch.

        A board row is sent as soon as it finishes and the pack is closed at
        the end, so the same task can appear in both calls.  Its result is
        counted once, on the call that accepted it first.
        """
        newly = 0
        for task in tasks:
            if task.result is None or task.uploaded:
                continue
            task.uploaded = True
            self.uploaded += 1
            newly += 1
        return newly

    def work_all_done(self, work: Work) -> bool:
        return self.work_role(work) is not None and work.all_done()

    def finish_work(self, work: Work) -> Work | None:
        """One work is fully uploaded.

        A finished PRIMARY promotes the ahead work, exactly as before.  A
        finished AHEAD just clears that slot: the primary keeps running, and
        `can_fetch_ahead` becomes true again so the next pack can be fetched.
        """
        if not getattr(work, "all_done", lambda: True)():
            raise RuntimeError("finish_work called on unfinished work")
        role = self.work_role(work)
        if role is None:
            return None
        if role == "primary":
            self.lease = None
            if self.queued is not None:
                self.lease, self.queued = self.queued, None
            self.sync_state("finish_work")
            return self.lease
        self.queued = None
        self.sync_state("finish_ahead")
        return None

    def give_back(self, note: str = "") -> None:
        if self.lease is None:
            # Even with no primary lease the ahead queue must be cleared:
            # leaving it on a verifier whose primary lease just lapsed would
            # stall the work on a dead device.
            if self.queued is not None:
                self.queued.aborted = True
                self.queued = None
            self._to(State.IDLE, f"give_back(no work):{note}")
            return
        self.lease.aborted = True
        self.last_error = note
        self.given_back += max(1, len(self.lease.tasks))
        self.lease = None
        if self.queued is not None:
            self.lease, self.queued = self.queued, None
        self.sync_state(f"give_back:{note}")

    # ------------------------------------------------------------- reporting
    @property
    def session_confirmed(self) -> int:
        """Points the server confirmed for this device SINCE THIS RUN STARTED."""
        if self.base_confirmed is None:
            return 0
        return max(0, self.confirmed_points - self.base_confirmed)

    @property
    def busy(self) -> bool:
        return self.state in (State.HOLDING, State.COMPUTING) or self.queued is not None

    def _self_view(self) -> dict:
        return {
            "id": self.vid,
            "state": self.state.value,
            "fetching_ahead": self.fetching_ahead,
            "tasks_held": (1 if self.lease else 0) + (1 if self.queued else 0),
            "queued_inflight": len(self.queued.inflight_tasks()) if self.queued else 0,
            "primary_inflight": len(self.lease.inflight_tasks()) if self.lease else 0,
            "tasks_ahead": self.tasks_ahead,
            "account_claimed": self.account_claimed,
            "claim_pending": self.claim_pending,
            "fetched": self.fetched,
            "uploaded": self.uploaded,
            "given_back": self.given_back,
            "session_confirmed": self.session_confirmed,
            "confirmed_points": self.confirmed_points,
        }

    def _work_view(self, work: Work | None, now: float) -> dict:
        if work is None:
            return {
                "kind": None, "lease": None, "source": None,
                "work_label": None, "done_tasks": 0, "total_tasks": 0,
                "pending_tasks": 0, "inflight_tasks": 0,
                "active_forced": False, "left_s": None,
                "renew_count": 0, "lost_rows": 0,
            }
        left = work.lease_left(now)
        return {
            "kind": work.kind,
            "lease": work.lease_id,
            "source": work.source,
            "work_label": work.label(),
            "done_tasks": sum(1 for t in work.tasks if t.state == "done"),
            "total_tasks": len(work.tasks),
            "pending_tasks": len(work.pending_tasks()),
            "inflight_tasks": len(work.inflight_tasks()),
            "active_forced": any(t.manual_priority for t in work.tasks),
            "left_s": None if left is None else round(left),
            "renew_count": work.renew_count,
            "lost_rows": work.lost_count(),
        }

    def _queued_view(self, q: Work | None, now: float) -> dict:
        if q is None:
            return {
                "queued": None, "queued_lease": None, "queued_source": None,
                "queued_tasks": 0, "queued_pending": 0,
                "queued_label": None, "queued_forced": False,
                "queued_left_s": None,
            }
        left = q.lease_left(now)
        done = sum(1 for t in q.tasks if t.state == "done")
        return {
            "queued": q.kind,
            "queued_lease": q.lease_id,
            "queued_source": q.source,
            "queued_tasks": len(q.tasks),
            "queued_done": done,
            "queued_pending": len(q.pending_tasks()),
            "queued_inflight": len(q.inflight_tasks()),
            "queued_label": q.label(),
            "queued_forced": any(t.manual_priority for t in q.tasks),
            "queued_left_s": None if left is None else round(left),
        }

    def snapshot(self, now: float | None = None) -> dict:
        """Flat view for the console; each section is built on its own."""
        now = now or time.time()
        snap: dict = {}
        snap.update(self._self_view())
        snap.update(self._work_view(self.lease, now))
        snap.update(self._queued_view(self.queued, now))
        snap.update({
            "banned_for_s": max(0, round(self.banned_until - now)),
            "riven_cooldown_s": max(0, round(self.riven_cooldown_until - now)),
            "spare": self.spare, "retiring": bool(self.retire_at),
            "backoff_s": round(self.backoff_s),
        })
        return snap
