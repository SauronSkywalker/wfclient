# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Unified per-verifier task status ledger.

The server's ``/api/board/mine`` and ``/api/appraise/mine`` responses are the
authoritative per-task states.  Everything the console shows about submitted
tasks is counted from this one ledger, so the top summary and each verifier row
cannot drift apart.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from operator import attrgetter

BOARD = "board"
RIVEN = "riven_gain"

SUBMITTED = "submitted"
WAITING = "waiting"
CHECKING = "checking"
CONFIRMED = "confirmed"
GONE = "gone"
LOCAL_INVALID = "local_invalid"
RETURNED = "returned"

PENDING_STATES = (SUBMITTED, WAITING, CHECKING)
TERMINAL_STATES = (CONFIRMED, GONE, LOCAL_INVALID, RETURNED)
MINE_MAX = 60


def _text(value) -> str:
    return "" if value is None else str(value)


def _raw_task(task) -> dict:
    raw = getattr(task, "task", task)
    return raw if isinstance(raw, dict) else {}


def _task_kind(task) -> str:
    raw = _raw_task(task)
    return _text(raw.get("kind") or getattr(task, "kind", BOARD) or BOARD)


def _board_key(identity: str, ruler: str, mode: str, question: str = "") -> str:
    if identity or ruler or mode:
        return f"board|{identity}|{ruler}|{mode}"
    return f"board|question|{question}"


def _riven_key(code: str, weapon: str, at: float) -> str:
    if code:
        return f"riven|{code}"
    try:
        return f"riven|{weapon}|{int(float(at) * 1000.0)}"
    except (TypeError, ValueError):
        return f"riven|{weapon}|0"


@dataclass
class TaskStatusRecord:
    verifier: str
    kind: str
    question: str = ""
    code: str = ""
    identity: str = ""
    ruler: str = ""
    mode: str = ""
    weapon: str = ""
    at: float = 0.0
    work: int = 0
    state: str = SUBMITTED
    uploaded: bool = False
    submitted_at: float = 0.0
    remote_at: float = 0.0
    confirmed_at: float = 0.0
    local_reason: str = ""

    @property
    def key(self) -> str:
        if self.kind == BOARD:
            return _board_key(self.identity, self.ruler, self.mode, self.question)
        return _riven_key(self.code, self.weapon, self.at)

    def pollable(self) -> bool:
        if self.kind == BOARD:
            return bool(self.identity and self.ruler and self.mode)
        return bool(self.code or (self.weapon and self.at))


def record_from_task(verifier: str, task, work: int = 0,
                     now: float | None = None) -> TaskStatusRecord:
    """Build one ledger record from a WorkTask or a raw task dictionary."""
    now = time.time() if now is None else float(now)
    raw = _raw_task(task)
    kind = _task_kind(task)
    record = TaskStatusRecord(verifier=verifier, kind=kind, work=int(work or 0))
    if kind == BOARD:
        record.question = _text(raw.get("question"))
        record.identity = _text(raw.get("identity"))
        record.ruler = _text(raw.get("ruler"))
        record.mode = _text(raw.get("mode") or (raw.get("record") or {}).get("mode"))
    else:
        record.code = _text(raw.get("code"))
        record.weapon = _text(raw.get("weapon") or (raw.get("request") or {}).get("weapon"))
        record.ruler = _text(raw.get("ruler"))
        record.at = now
    return record


class TaskStatusLedger:
    """Bounded, in-memory status book for submitted board and riven tasks."""

    def __init__(self, per_verifier: int = 20000, hard_limit: int = 50000):
        self.per_verifier = max(1, int(per_verifier))
        self.hard_limit = max(self.per_verifier, int(hard_limit))
        self.query_limit = min(MINE_MAX, self.per_verifier)
        self._records: dict[str, dict[str, TaskStatusRecord]] = {}

    def _bucket(self, verifier: str) -> dict[str, TaskStatusRecord]:
        return self._records.setdefault(verifier, {})

    def _remember(self, record: TaskStatusRecord, now: float) -> TaskStatusRecord:
        bucket = self._bucket(record.verifier)
        old = bucket.get(record.key)
        if old is not None:
            return old
        record.submitted_at = now
        bucket[record.key] = record
        self._trim(bucket)
        return record

    def _trim(self, bucket: dict[str, TaskStatusRecord]) -> None:
        if len(bucket) <= self.per_verifier:
            return
        terminal = [r for r in bucket.values() if r.state in TERMINAL_STATES]
        if terminal:
            oldest = min(terminal, key=attrgetter("submitted_at"))
            bucket.pop(oldest.key, None)
            return
        if len(bucket) <= self.hard_limit:
            return
        oldest = min(bucket.values(), key=attrgetter("submitted_at"))
        bucket.pop(oldest.key, None)

    def _record(self, verifier: str, task, work: int = 0,
                now: float | None = None) -> TaskStatusRecord:
        now = time.time() if now is None else float(now)
        wanted = record_from_task(verifier, task, work, now)
        bucket = self._bucket(verifier)
        return bucket.get(wanted.key) or self._remember(wanted, now)

    def mark_submitted(self, verifier: str, task, work: int = 0,
                       now: float | None = None) -> TaskStatusRecord:
        now = time.time() if now is None else float(now)
        record = self._record(verifier, task, work, now)
        if record.state in (CONFIRMED, GONE):
            return record
        if work:
            record.work = int(work)
        record.state = SUBMITTED
        record.uploaded = True
        record.local_reason = ""
        record.submitted_at = record.submitted_at or now
        return record

    def mark_local_failure(self, verifier: str, task, reason: str,
                           now: float | None = None) -> TaskStatusRecord:
        now = time.time() if now is None else float(now)
        record = self._record(verifier, task, 0, now)
        if record.state not in (CONFIRMED, GONE):
            record.state = LOCAL_INVALID
            record.local_reason = _text(reason)
            record.remote_at = now
        return record

    def mark_returned(self, verifier: str, work, reason: str,
                      now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        changed = 0
        for task in getattr(work, "tasks", []) or []:
            record = self._record(verifier, task, 0, now)
            if record.uploaded:
                continue
            if record.state not in (CONFIRMED, GONE, LOCAL_INVALID):
                record.state = RETURNED
                record.local_reason = _text(reason)
                record.remote_at = now
                changed += 1
        return changed

    def mark_work_failure(self, verifier: str, work, reason: str,
                          now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        changed = 0
        for task in getattr(work, "tasks", []) or []:
            if getattr(task, "uploaded", False) or getattr(task, "result", None) is None:
                continue
            record = self.mark_local_failure(verifier, task, reason, now)
            if record.state == LOCAL_INVALID:
                changed += 1
        return changed

    def _set_remote(self, record: TaskStatusRecord, item: dict,
                    now: float) -> None:
        state = _text(item.get("state"))
        if state not in PENDING_STATES and state not in (CONFIRMED, GONE):
            record.state = LOCAL_INVALID
            record.local_reason = f"unknown remote state: {state}"
            record.remote_at = now
            return
        record.state = state
        record.remote_at = now
        if state == CONFIRMED:
            record.confirmed_at = now

    def apply_board_mine(self, verifier: str, response: dict,
                         now: float | None = None) -> int:
        if not isinstance(response, dict) or not response.get("ok"):
            return 0
        now = time.time() if now is None else float(now)
        bucket = self._bucket(verifier)
        changed = 0
        for item in response.get("orders") or []:
            if not isinstance(item, dict):
                continue
            key = _board_key(_text(item.get("identity")), _text(item.get("ruler")),
                             _text(item.get("mode")))
            record = bucket.get(key)
            if record is None:
                continue
            before = record.state
            self._set_remote(record, item, now)
            if record.state != before:
                changed += 1
        return changed

    def apply_appraise_mine(self, verifier: str, response: dict,
                            now: float | None = None) -> int:
        if not isinstance(response, dict) or not response.get("ok"):
            return 0
        now = time.time() if now is None else float(now)
        bucket = self._bucket(verifier)
        changed = 0
        for item in response.get("tasks") or []:
            if not isinstance(item, dict):
                continue
            code = _text(item.get("code"))
            weapon = _text(item.get("weapon"))
            try:
                at = float(item.get("at") or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            key = _riven_key(code, weapon, at)
            record = bucket.get(key)
            if record is None and code:
                record = bucket.get(_riven_key("", weapon, at))
            if record is None and not code:
                for rec in bucket.values():
                    if getattr(rec, "identity", "") == _text(item.get("identity")):
                        record = rec
                        break
            if record is None:
                continue
            before = record.state
            self._set_remote(record, item, now)
            if record.state != before:
                changed += 1
        return changed

    @staticmethod
    def _poll_key(record: TaskStatusRecord) -> tuple[float, float]:
        """Oldest unacknowledged rows first, rotating past the 60-row limit."""
        return (record.remote_at or 0.0, record.submitted_at or 0.0)

    def _open_records(self, verifier: str, kind: str) -> list[TaskStatusRecord]:
        records = [r for r in self._bucket(verifier).values()
                   if r.kind == kind and r.state in PENDING_STATES and r.pollable()]
        records.sort(key=self._poll_key)
        return records[:self.query_limit]

    def open_orders(self, verifier: str) -> list[dict]:
        return [{"identity": record.identity, "ruler": record.ruler,
                 "mode": record.mode}
                for record in self._open_records(verifier, BOARD)]

    def board_state(self, verifier: str, identity: str,
                    ruler: str, mode: str) -> str | None:
        """Latest `/mine` state for one board row, or None if never asked."""
        record = self._bucket(verifier).get(_board_key(identity, ruler, mode))
        return record.state if record is not None else None

    def requeue_board(self, verifier: str, identity: str, ruler: str,
                      mode: str, now: float | None = None) -> bool:
        """A `gone` answer we just re-sent: poll it again instead of forgetting."""
        now = time.time() if now is None else float(now)
        record = self._bucket(verifier).get(_board_key(identity, ruler, mode))
        if record is None or record.state != GONE:
            return False
        record.state = SUBMITTED
        record.uploaded = True
        record.local_reason = ""
        record.remote_at = now
        return True

    def open_riven_tasks(self, verifier: str) -> list[dict]:
        return [{"code": record.code, "weapon": record.weapon, "at": record.at}
                for record in self._open_records(verifier, RIVEN)]

    def pending_work(self, verifier: str | None = None) -> int:
        """Uploaded work not yet confirmed, gone, returned or locally rejected."""
        buckets = (self._bucket(verifier),) if verifier is not None else self._records.values()
        total = 0
        for bucket in buckets:
            for record in bucket.values():
                if record.uploaded and record.state in PENDING_STATES:
                    total += record.work
        return total

    def pending_points(self, verifier: str | None = None) -> int:
        return self.pending_work(verifier) // 1_000_000_000

    def pending_points_of(self, verifiers) -> int:
        return sum(self.pending_points(verifier) for verifier in verifiers)

    def _blank(self) -> dict[str, int]:
        return {SUBMITTED: 0, WAITING: 0, CHECKING: 0, CONFIRMED: 0,
                GONE: 0, LOCAL_INVALID: 0, RETURNED: 0}

    def _tally(self, out: dict[str, int],
               bucket: dict[str, TaskStatusRecord]) -> None:
        for record in bucket.values():
            if record.state in out:
                out[record.state] += 1

    def _finish(self, out: dict[str, int]) -> dict[str, int]:
        out["pending"] = out[SUBMITTED] + out[WAITING] + out[CHECKING]
        out["invalid"] = out[GONE] + out[LOCAL_INVALID]
        out["total"] = sum(out[k] for k in (
            SUBMITTED, WAITING, CHECKING, CONFIRMED,
            GONE, LOCAL_INVALID, RETURNED))
        return out

    def counts(self, verifier: str | None = None) -> dict[str, int]:
        out = self._blank()
        if verifier is None:
            for bucket in self._records.values():
                self._tally(out, bucket)
        else:
            self._tally(out, self._bucket(verifier))
        return self._finish(out)

    def counts_of(self, verifiers) -> dict[str, int]:
        out = self._blank()
        for verifier in verifiers:
            self._tally(out, self._bucket(verifier))
        return self._finish(out)

    def snapshot(self, verifier: str | None = None) -> dict:
        return {"counts": self.counts(verifier)}
