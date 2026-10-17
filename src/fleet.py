# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""The fleet: the verifiers, the pool, the compute slots, and the three rules.

RULES (the design's own words)
  1. `active_verifiers` verifiers fetch by default (3).
  2. While the pool holds fewer than `riven_floor` riven tasks, more verifiers
     are woken — up to `max_verifiers`.
  3. When the pool holds `riven_floor` or more riven tasks, nothing new is
     fetched; a verifier that has just uploaded goes idle-but-online (it simply
     stops asking — there is no server call in that).
  4. A free slot takes a BOARD task before a riven one.
  5. Optional one-ahead queue: a verifier may hold its current Work plus one
     `ahead:true` Work.  A board Work is a pack of rows; a riven Work is one task.

The engine processes are the compute side: one per slot, because the native
`/api/optimize` refuses a second job while one is active (`web/src/main.rs:439`)
— so three riven tasks at once need three processes, eight lanes each.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import functools
import json
import operator
import os
import pathlib
import random
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import config
import cores
import paths
import board_sync
from api import Engine, Remote, RowRefused, board_payload_from_result
from board_sync import SyncDials
from task_status import TaskStatusLedger
from task_log import TaskLogger
from verifier_fsm import State, Verifier
from work_model import (BOARD_LEASE_SAFE_S, CONSENT_V, Work, WorkTask,
                        answer_payload, work_from_response)

def new_verifier_id() -> str:
    """A WFSim-recognised client id: exactly 24 lowercase hex characters.

    The server now distinguishes a recognised client from an arbitrary
    device id with `^[0-9a-f]{24}$`; the old 20-character alphabet id is still
    accepted by the generic `VERIFIER_ID` syntax but is refused as
    `unrecognised_client`.
    """
    return secrets.token_hex(12)


def utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# TWO POOLS, BECAUSE THE WORK HAS TWO SHAPES.
#
# `asyncio.to_thread` puts EVERYTHING on one default pool (`min(32, cpu+4)` = 28
# threads here), and a riven search occupies one of those threads for its whole
# 27-49 seconds. Three slots did that, leaving 25 threads to serve 29 fetches and
# every upload — so a fetch could not start until a search finished, and the
# verifier sat in FETCHING. The pools below are sized to their work and never
# wait on each other:
#   NET_POOL    — many, short (a fetch is ~5s, an upload ~0.2s)
#   ENGINE_POOL — few, LONG (one engine call owns its core budget for minutes)
# A LEASE IS A HEARTBEAT NOW, not a deadline (worker/tasks.js `renewRow`): a
# row still being worked on is renewed and never cut off, and only a silent
# client loses one.  These are the official page's numbers.
RENEW_LEAD_S = 120.0        # start renewing this long before a row lapses
RENEW_RETRY_S = 30.0        # a missed beat is not a refusal
#: A RIVEN LEASE SAYS NOTHING ABOUT ITS LENGTH: the sync hand-out carries no
#: `lease_ms` (only kind/lease/code/weapon/ruler/context) and `/renew` answers
#: a bare `{ok:true, held:true|false}`.  So the beat has to be short enough for
#: the SHORTEST plausible lease, and the first one has to fire at once.
#: Measured, 2026-10-15: two rivens were handed out and both were already gone
#: when the first beat fired 45 s later (and a third was gone at 28 s) — the
#: server hands out leases whose clock is already running, and a first beat
#: that waits is a coin toss.  Renewing is idempotent and the server merges
#: with `max(lease_until, now + renew_ms)`, so an immediate beat costs one
#: request and re-anchors the lease to now; 20 s thereafter rides inside even
#: a 30 s lease.
RIVEN_RENEW_S = 20.0        # the pulse: short enough for any plausible lease
RIVEN_RENEW_FIRST_S = 2.0   # anchor beat, fired the moment the work arrives
RIVEN_LEASE_S = 120.0       # assumed lease when the reply says no `ms`
#: BOARD-ONLY: after a riven is given back, stay off the riven door this long.
#: The server sets the verifier's `next_at` to `now + sync_floor_ms` (60 s) when
#: it hands a question out alone (worker/sync.js `alone`), so anything asked
#: before that is answered `early` and hands out nothing — 70 s clears it with
#: a margin, and keeps a client that the pool has nothing else for from
#: hammering the door every beat.
RIVEN_REFUSE_RETRY_S = 70.0
#: SPARE VERIFIER: the residents must hold the slots this long, continuously,
#: before a spare stands down — a shorter breather (one 70 s riven cooldown,
#: say) must not flap the spare on and off.
SPARE_RETIRE_STABLE_S = 30.0
#: A ROW THAT HAS WAITED THIS LONG FOR AN ENGINE is put at the front of the
#: pick order, at the manual pick's own level.  Renewals keep a row alive
#: forever, so without this a pack that arrives faster than it drains can
#: starve one row until the server's row ceiling takes it.  The client's config
#: key is `stale_start_promote_seconds` (0 disables it; a missing key means
#: this default).
STALE_START_S = 2400.0
#: A FAILED ANSWER SEND IS RETRIED ON THIS LADDER — the page's own
#: (`SEND_AGAIN_MS = [3 s, 10 s, 30 s]`, 69-board-work.js).  The worker answers a
#: batch in ~4.5 s and a transient 5xx or a dropped connection is ordinary; the
#: alternative to waiting is handing a pack of finished rows back.
SEND_AGAIN_S = (3.0, 10.0, 30.0)
#: SENT BOARD ANSWERS ARE KEPT IN MEMORY UNTIL THE SERVER CONFIRMS THEM.
#: `/answers` replies `{ok:true}` whether the row was recorded or silently
#: dropped (a lapsed lease), so `uploaded` only means received.  Each kept
#: answer is a few hundred bytes; even a 200-row pack is negligible next to
#: the folds on the engines.  Memory only, deliberately no disk: this client
#: runs for weeks and restarts are rare, and losing one or two unconfirmed
#: rows across a restart beats steady disk writes.
#: Deleted when `/mine` says confirmed/gone, when the row is lost, or after
#: BOARD_KEPT_TTL_S.  A `gone` answer under a still-live lease is re-sent
#: BOARD_KEPT_RESENDS times, then dropped as unrecordable.
BOARD_KEPT_MAX = 5000
BOARD_KEPT_TTL_S = 7200.0
BOARD_KEPT_RESENDS = 2
#: HOW LONG AN UNANSWERED RESULT MAY WAIT FOR THE SERVER'S PACING TICK.  The
#: sync that carries answers is also the ask, and the server only hands a pack
#: to a sync it does not call `early` — so `_sync_due` holds the answers until
#: `next_ms` rather than firing every 3 s into an `early: true`.  A real
#: `next_ms` is tens of seconds; this is only a backstop.
RESULTS_HOLD_MAX_S = 90.0
#: `states` (sync body) — the wire's own cap.  Asking "where does this result
#: stand" about more than this is malformed; the client used to send one entry
#: per owed answer with no limit at all.
SYNC_STATES_MAX = 100
#: HOW LONG A FINISHED BOARD ANSWER MAY WAIT FOR A QUEUED PACK, when the config
#: key `board_submit_hold_ms` is missing or unreadable.  One hour — the anchor's
#: budget, deliberately well inside the server's own `row_ceiling_ms` (2 h), past
#: which another client may take an outstanding row anyway.  It ends early for
#: exactly one reason: the anchor was TAKEN from us, so there is nothing left to
#: renew.  See `_sync_results_with_anchor`.
BOARD_SUBMIT_HOLD_S = 3600.0
#: ASK AGAIN AFTER AN EMPTY ANSWER.  The page waits `ASK_EVERY_MS = 10 s`
#: (69-board-work.js; docs/CLIENTS.md §"PACE"); asking at once, which this used
#: to do, spends a round trip (measured 1.76 s) to be told the same thing, and a
#: client asking 3.5x the reference's rate is what the fair-share bucket is for.
#: The wait ESCALATES across consecutive empties and resets the moment work
#: arrives, so the end of an idle spell is still found within one beat.
EMPTY_ASK_S = 5.0
EMPTY_ASK_MAX_S = 10.0
#: A FINISHED RIVEN ANSWER IS KEPT IN MEMORY BEFORE IT IS SENT, and re-sent
#: until the server says it took it.  A riven answer is the most expensive thing
#: this client produces (minutes of search) and the only one with no second
#: chance: a dropped connection used to throw it away.  The lease is held (and
#: renewed) while the kept answer waits, so the server still accepts it.
#: Memory only, deliberately no disk: this client runs for weeks, restarts are
#: rare, and losing one unconfirmed answer across a restart beats steady disk
#: writes.
RIVEN_PENDING_EVERY_S = 15.0     # how often the retry loop looks
RIVEN_PENDING_KEEP_S = 600.0     # give up — and release the work — after this
RIVEN_PENDING_PER_PASS = 3        # one pass sends at most this many, so a backlog
                                 # cannot stall the loop that renews leases
RENEW_PER_PASS = 4          # a 20-row pack renews over a few passes, not a burst
#: How long ONE lease release may take on the way out.  `Remote` defaults to
#: 120 s, which is far too long when there are thirty of them to send.
SHUTDOWN_RELEASE_S = 8

NET_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="net")
ENGINE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="engine")

#: A VERIFIER THAT CANNOT ASK IS DEAD UNTIL THE PROCESS RESTARTS.  It has only
#: two ways to fetch — a plain ask from `IDLE`, and an `ahead` ask while it
#: holds a live lease — so a verifier wedged between the two (a finished or
#: aborted work left attached, or a fetch that ended without a verdict and left
#: the FSM in `FETCHING`) never asks again and nothing repaired it.  This beat
#: notices and resets one; the client's config key is
#: `verifier_watchdog_seconds` (missing means this default).
VERIFIER_WATCHDOG_S = 10.0
#: HOW LONG A FINISHED ANSWER MAY HOLD A VERIFIER.  Renewals extend the keep
#: window for as long as the lease lives, so a permanently broken confirmation
#: path (every `appraise/mine` failing, say) would otherwise park one verifier
#: for ever.  Fifteen minutes is ~60 re-sends; the config key is
#: `riven_pending_hold_seconds` (0 disables, missing means this default).
RIVEN_PENDING_HOLD_S = 900.0
#: A SLOT WEDGED ON ONE ROW HAS NO PROGRESS CHANNEL: a board row is one blocking
#: engine call, so "has it been long enough" cannot say whether the engine is
#: working or stuck.  CPU TIME CAN: a computing engine always adds it, a wedged
#: one stops.  Sample one process every `slot_stall_seconds` (default 60) and
#: rebuild the slot after this many consecutive samples with no progress.
SLOT_STALL_EVERY_S = 60.0
SLOT_STALL_CHECKS = 3
SLOT_STALL_CPU_EPS = 0.05


def claim_label(v: Verifier) -> str:
    """Keep the server-side device labels in the existing wfsimpp NN form."""
    label = (v.label or "").strip()
    if label.startswith("v") and label[1:].isdigit():
        return f"wfsimpp {label[1:]}"
    return label or f"wfsimpp {v.vid[:6]}"


def task_label(item) -> str:
    """What a Work or WorkTask is, in one line — public facts only."""
    if isinstance(item, WorkTask) or isinstance(item, Work):
        return item.label()
    task = getattr(item, "task", None) or {}
    weapon = (task.get("record") or {}).get("weapon") or task.get("weapon") or "?"
    ruler = (getattr(item, "ruler", "") or "").replace("standard_", "")
    kind = getattr(item, "kind", "board")
    return f"{'riven' if kind == 'riven_gain' else 'board'} {weapon}/{ruler}"


def _hour_of(hour: dict) -> str:
    return str((hour or {}).get("hour") or "")


def _hour_holder(hours: list) -> bool | None:
    if not hours:
        return None
    holder = max(hours, key=_hour_of).get("holder")
    return {"you": True, "other": False, "device": True}.get(holder)


def _summary_holder(summary: dict) -> bool | None:
    try:
        you = int(summary.get("you") or 0)
        other = int(summary.get("other") or 0)
    except Exception:
        you = other = 0
    if you and not other:
        return True
    if other and not you:
        return False
    return None


def _noop_event(kind: str, text: str) -> None:
    return None


def _noop_config_changed() -> None:
    return None


def _task_due(task, default: float) -> float:
    return task.row_until or default


@dataclass
class SlotState:
    """WHAT ONE ENGINE IS DOING, in the dispatcher's own hands.

    With the upload off the critical path, the task being COMPUTED and the task
    being UPLOADED are two different tasks, so both are named here. Nothing in
    the UI has to infer anything from timing.
    """
    slot: int
    port: int
    verifier: str | None = None          # whose task is on the engine now
    kind: str | None = None
    lease: str | None = None
    source: str | None = None             # "fetch" | "ahead"
    label: str = ""
    started_at: float = 0.0
    #: same moment on the MONOTONIC clock: `time.time()` can step backwards
    #: (NTP correction) and a wall-clock elapsed would go negative for every
    #: task started before the step — the UI showed "已用 -180s" after one.
    started_mono: float = 0.0
    uploading: str | None = None         # verifier whose answer is still in flight
    uploading_lease: str | None = None   # which of that verifier's works it is
    last_verifier: str | None = None
    last_label: str = ""
    last_ok: bool | None = None
    last_finished_at: float = 0.0
    done: int = 0
    last_counts: dict = field(default_factory=dict)
    #: The second task held by this verifier via `ahead:true`.  The dispatcher
    #: records it on the engine that is currently running the verifier's first
    #: task, so the UI and race logs never have to infer it from fetch timing.
    next_verifier: str | None = None
    next_kind: str | None = None
    next_lease: str | None = None
    next_label: str = ""
    next_deadline: float = 0.0
    pack_lease: str | None = None
    question: str | None = None
    task_index: int | None = None
    task_total: int | None = None
    #: lanes the CURRENT task is running with (live, not the configured plan)
    lanes: int = 0
    #: how far the task on the engine has got. Written from the worker thread the
    #: engine call runs on, read from the event loop — a plain dict assignment,
    #: which the GIL makes atomic enough for a progress readout.
    progress: dict = field(default_factory=dict)

    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        return {
            "slot": self.slot, "port": self.port,
            "verifier": self.verifier, "kind": self.kind, "label": self.label,
            "lanes": self.lanes,
            "source": self.source,
            "elapsed": (round(max(0.0, time.monotonic() - self.started_mono), 1)
                        if self.started_mono else None),
            "uploading": self.uploading,
            "uploading_lease": self.uploading_lease,
            "last_verifier": self.last_verifier, "last_label": self.last_label,
            "last_ok": self.last_ok,
            "last_ago": (round(max(0.0, now - self.last_finished_at), 1)
                         if self.last_finished_at else None),
            "done": self.done,
            "last_counts": dict(self.last_counts),
            "next_verifier": self.next_verifier,
            "next_kind": self.next_kind,
            "next_lease": self.next_lease,
            "next_label": self.next_label,
            "next_left_s": (round(self.next_deadline - now)
                            if self.next_deadline else None),
            "pack_lease": self.pack_lease,
            "question": self.question,
            "task_index": self.task_index,
            "task_total": self.task_total,
            "next": ({
                "verifier": self.next_verifier,
                "lease": self.next_lease,
                "kind": self.next_kind,
                "label": self.next_label,
                "left_s": (round(self.next_deadline - now)
                           if self.next_deadline else None),
            } if self.next_lease else None),
            "progress": dict(self.progress),
        }


class Fleet:
    def __init__(self, cfg: dict, on_event=None, on_config_changed=None):
        self._init_identity(cfg, on_event, on_config_changed)
        self._init_runtime()
        self._init_books(cfg)

    def _init_identity(self, cfg: dict, on_event, on_config_changed) -> None:
        self.cfg = cfg
        self.on_event = on_event or _noop_event
        self.on_config_changed = on_config_changed or _noop_config_changed
        # HAND THE LEVEL THROUGH AS-IS: `TaskLogger` reads the four levels
        # ("off"/"standard"/"verbose"/"full") itself, and 关 is the default.
        # Wrapping it in `bool()` here collapsed every level to on/off, so the
        # settings page's 标准/详细/完整 all produced the same file.
        self.log = TaskLogger(paths.ROOT,
                              enabled=cfg.get("task_logging", "off"))
        self.remote = Remote(logger=self.log)
        self.engine_id: str | None = None
        #: the id sent on the wire.  Normally the installed core's id; the user
        #: may select an older accepted id, but only after a full pause drain.
        self.wire_engine: str | None = None
        #: the latest GitHub catalog: current/accepted/CI-only engine ids.
        self.engine_catalog: dict | None = None
        #: the engine id recorded beside the core actually installed on disk.
        self.local_engine: str | None = None
        self.release: str | None = None
        #: what release.json says the server is serving RIGHT NOW.  Display only.
        self.served_engine: str | None = None
        self.verifiers: list[Verifier] = []
        self.engines: list[Engine] = []
        #: one Job Object shared by every engine, so the CPU cap is a TOTAL cap.
        self.cpu_limit = None
        self.cpu_limit_actual: int | None = None
        #: None = not tried yet, False = the server has no /renew (old server)
        self.renew_supported: bool | None = None
        #: how many leases the last stop() returned to the server
        self.returned_on_stop = 0
        self._stopped = False
        #: how long the LAST riven took, in ms — the next one's ahead yardstick.
        self.last_riven_ms = 0.0
        #: the line that explains the current core-type choice
        self.core_type_note: str = ""

    def _init_runtime(self) -> None:
        self.slot_task: dict[int, str | None] = {}      # slot -> verifier id
        #: (verifier, lease, task index) selected by the user.
        self.forced_task: tuple[str, str, int] | None = None
        #: when `_retry_pending_riven` may run again
        self._pending_riven_at: float = 0.0
        #: when `_retry_stalled_board` may run again
        self._stalled_board_at: float = 0.0
        #: verifier vid -> when its answers FIRST went on hold for a queued pack
        #: (the console's 等待中 timer).  Cleared the moment a hand-in goes
        #: through, so it always measures the current wait.
        self._submit_held_since: dict[str, float] = {}
        #: when the residents FIRST covered every slot (spare retirement needs
        #: that state to hold for `SPARE_RETIRE_STABLE_S` before standing down)
        self._spare_covered_since: float = 0.0
        #: riven leases lost recently (timestamps) and the cooldown that follows
        #: a run of them — a search that keeps coming back `held:false` costs
        #: minutes of a verifier for nothing, so the door is shut for a while.
        self._riven_lost_at: list = []
        self._riven_refuse_until: float = 0.0
        #: board wire negotiation (protocol 8 dual-stack, docs/CLIENTS.md).
        #: `_proto_served` is the union of `protocols_served` seen on hand-out
        #: replies, sync replies, the work signal and release.json.
        self._proto_served: list = [8]
        #: THE SERVER HAS REFUSED PROTOCOL 8 (`fallback: 7`).  This client speaks
        #: ONE board wire — the sync — and the classic `/work` /`/answers` /
        #: `/renew` /`/release` doors are gone from it, so there is nothing to
        #: fall back TO: a server that will not serve 8 leaves this client with
        #: nothing to do, and it rests rather than half-switching to a wire the
        #: rest of the code no longer keeps in step.
        self._proto8_off: bool = False
        #: authoritative protocol-8 dials (doc defaults until synced).
        self._sync_dials = SyncDials()
        #: verifier id -> timestamp of the next work-carrying sync.  THIS IS
        #: PACING (`next_ms` from the last reply), never a health signal.
        self._sync_next: dict[str, float] = {}
        #: verifier id -> the ONE row its pack keeps back for a queued pack:
        #: `{lease, question, released, at}`.  Tracked by identity, because the
        #: whole policy turns on whether THAT row is still ours — see
        #: `_sync_results_with_anchor`.
        self._anchor: dict[str, dict] = {}
        #: verifier id -> end of the transport-error backoff.  This — and
        #: `_sync_fails` — is the only thing that makes a verifier "unhealthy".
        #: Keeping the two apart is the whole point: `next_ms` can be minutes,
        #: and reading pacing as failure used to reopen the protocol-7 doors
        #: (`/mine`, `/answers`, `/renew`) on every successful sync.
        self._sync_backoff_until: dict[str, float] = {}
        #: verifier id -> last sync timestamp (any kind).
        self._sync_last: dict[str, float] = {}
        #: verifier id -> consecutive sync transport failures (backoff).
        self._sync_fails: dict[str, int] = {}
        #: verifier id -> hour -> {asks, failed, lanes} (memory only).
        self._sync_idle: dict = {}
        #: verifier id -> refused rows awaiting the next sync.
        self._sync_refused: dict[str, list] = {}
        #: last work-signal body and when it was fetched.
        self._signal_cache: dict | None = None
        self._signal_at: float = 0.0
        #: verifier id -> sync the work signal asked to wait until.
        self._signal_wait_until: dict[str, float] = {}
        self._signal_failed_until: float = 0.0
        self.running = False
        self.paused = False
        #: true while a rebuilt core is replacing the old one.
        self.maintenance = False
        #: every fetch/upload task in flight, so shutdown can drain them
        self.tasks: set = set()
        #: verifier id -> the in-flight `/api/board/work` task.
        self.fetch_tasks: dict[str, asyncio.Task] = {}
        #: one long-lived task per compute slot.
        self.slot_tasks: list[asyncio.Task] = []
        #: supervisor task per compute slot.
        self.slot_supervisors: dict[int, asyncio.Task] = {}
        #: consecutive watchdog ticks a slot has been idle with work available
        self.slot_idle_count: dict[int, int] = {}
        #: only pick_for_slot + start_task are held under this lock.
        self.pick_lock = asyncio.Lock()
        #: one board-answer flush per lease at a time.
        self.upload_locks: dict[str, asyncio.Lock] = {}
        #: riven leases with a `/result` POST on the wire right now.  The
        #: periodic re-send must never duplicate one that is still in flight:
        #: the late duplicate answers `first:false` even when the first copy
        #: was recorded, which used to look like a lost answer.
        self.riven_sending: set[str] = set()
        #: slots whose engine is being restarted to abort an expired board fold
        self.slot_restarting: set[int] = set()
        #: global fetch concurrency cap (plain + ahead)
        self.fetching_count: int = 0
        #: WHICH SLOTS ARE ON A RIVEN — display only.
        self.riven_slots: set[int] = set()
        #: lanes freed by slots shut down while draining a pause
        self.drained_lanes: int = 0
        #: slots whose engine is shut down because the pause left it no work
        self.stopped_slots: set[int] = set()
        #: 2.4 IDLE RECYCLE: slots whose engine was reclaimed after a long dry
        #: spell (batches are minutes apart, and an idle engine still holds its
        #: loaded tables).  DELIBERATELY NOT `stopped_slots`: that set belongs
        #: to the pause machinery, which moves lanes around and is cleared by
        #: `resume_from_pause`.  This one only gives memory back, and
        #: `_idle_wake` undoes it the moment work is reserved for the slot.
        self.idle_stopped: set[int] = set()
        #: slot -> (lease, cpu_seconds, sampled_at, consecutive_no_progress)
        self.slot_cpu_sample: dict[int, tuple[str, float, float, int]] = {}
        #: one record per engine: what it is computing, for whom, since when
        self.slots: list[SlotState] = []
        #: per-slot thread budget; filled in start().
        self.slot_lanes: list[int] = []
        #: per-slot CPU Set ids for `max` mode (balanced P/E groups)
        self.slot_cpu_sets: list[list[int]] = []
        #: per-slot CPU Set id of the slot's fastest core, for single-thread work
        self.slot_primary_sets: list[int] = []
        #: per-slot logical processor number of that fastest core
        self.slot_primary: list[int] = []
        #: per-slot logical processor numbers, for affinity pin/unpin
        self.slot_cpus: list[list[int]] = []
        self.t0 = time.time()

    def _init_books(self, cfg: dict) -> None:
        self.stats = {"uploaded": 0, "given_back": 0, "failed": 0,
                      "invalid": 0, "returned": 0,
                      "board": 0, "riven": 0, "compute_s": 0.0}
        self.task_status = TaskStatusLedger()
        #: one delivery lock per verifier — see `_deliver_lock`.
        self._deliver_locks: dict[str, asyncio.Lock] = {}
        #: sent-but-unconfirmed board answers, (lease, question) -> entry.
        #: Memory only (see BOARD_KEPT_*): deleted on mine confirmed/gone,
        #: on row loss, or after BOARD_KEPT_TTL_S.
        self._board_kept: dict[tuple[str, str], dict] = {}
        #: finished-but-unconfirmed riven answers, lease -> entry.
        #: Memory only (see RIVEN_* above): the retry loop re-sends until the
        #: server takes them; a restart drops whatever is still waiting.
        self._riven_kept: dict[str, dict] = {}
        #: (time, confirmed points) samples, for the points-per-minute readout
        self.points_history: collections.deque = collections.deque(maxlen=600)
        self.points_at: float = 0.0
        #: observed board-row wall time; the server uses this as pace_ms.
        self.pace_ms: int = max(100, int(cfg.get("pace_ms_default") or 1000))
        #: the ACCOUNT's own total, from /api/account/devices.
        self.account_points: int | None = None
        self.account_at: float = 0.0
        #: points cursor sent with protocol-8 syncs (`since`): the reader's own
        #: "today", same value the official page and `/board/points` use.
        #: A sync reply's `points` then replaces the per-device poll for the
        #: verifiers it covers.
        self._points_since: str | None = None
        #: verifier id -> last sync that carried a `points` body.
        self._sync_points_at: dict[str, float] = {}
        #: Lightweight login health, independent of the heavy points aggregate.
        self.account_signed_in: bool | None = None
        self.account_info_at: float = 0.0
        #: PUBLIC CONTRIBUTOR RANKING, refreshed every 60 seconds.
        self.leaderboard: list[dict] = []
        self.leaderboard_base: dict[str, tuple[int, float]] = {}
        self.leaderboard_computing: int = 0
        self.leaderboard_total: int = 0
        self.leaderboard_at: float = 0.0
        self.leaderboard_error: str = ""

    def note_invalid(self, n: int = 1) -> None:
        self.stats["failed"] += int(n)

    def note_returned(self, n: int = 1) -> None:
        self.stats["given_back"] += int(n)

    # ------------------------------------------------------------------ pools
    async def _io(self, fn, *a, **kw):
        """A network call — many of these at once, and none of them may wait on a
        search."""
        return await asyncio.get_running_loop().run_in_executor(
            NET_POOL, functools.partial(fn, *a, **kw))

    async def _cpu(self, fn, *a, **kw):
        """An engine call. Long, and it owns a core budget; it must not be able
        to starve the network."""
        return await asyncio.get_running_loop().run_in_executor(
            ENGINE_POOL, functools.partial(fn, *a, **kw))

    # ------------------------------------------------------------------ setup
    def _local_midnight_utc_hour(self) -> str:
        now = datetime.datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H")

    async def _account_device_prefixes(self) -> set[str] | None:
        """Current account's six-character device prefixes, or None if unknown."""
        try:
            acc = await asyncio.wait_for(self._io(self.remote.account_devices), timeout=15)
        except Exception as e:
            self.on_event("verifiers", f"归属校验：设备列表不可用，保留现有 verifier：{type(e).__name__}: {e}")
            return None
        if not isinstance(acc, dict) or not acc.get("ok"):
            self.on_event("verifiers", "归属校验：设备列表异常，保留现有 verifier")
            return None
        return {str(d.get("id")) for d in (acc.get("devices") or [])
                if isinstance(d, dict) and d.get("id")}

    async def _read_verifier_points(self, vid: str, since: str) -> dict | None:
        try:
            return await asyncio.wait_for(
                self._io(self.remote.points, vid, since), timeout=10)
        except Exception:
            return None

    async def _read_verifier_ledger(self, vid: str, since: str) -> dict | None:
        try:
            return await asyncio.wait_for(
                self._io(self.remote.board_ledger, vid, since), timeout=12)
        except Exception:
            return None

    @staticmethod
    def _ledger_holder(led: dict) -> bool | None:
        """True = this account, False = another account, None = undecided."""
        hours = [h for h in (led.get("hours") or []) if isinstance(h, dict)]
        holder = _hour_holder(hours)
        if holder is not None:
            return holder
        return _summary_holder(led.get("summary") or {})

    async def _verifier_ownership(self, vid: str, owned: set[str] | None,
                                  since: str) -> bool | None:
        """True = current/unowned, False = another account, None = unknown.

        The full verifier is the primary key: `/api/board/points` says whether
        some account holds it, and `/api/board/ledger` labels its hours as
        `you`, `other`, or `device`.  The account's six-character list is only
        a fallback when the ledger has no hour to label yet.
        """
        r = await self._read_verifier_points(vid, since)
        if not isinstance(r, dict) or not r.get("ok"):
            return None
        if not r.get("claimed"):
            return True
        # Held by some account: ask the full-id ledger who held its hours.
        led = await self._read_verifier_ledger(vid, since)
        if isinstance(led, dict) and led.get("ok"):
            holder = self._ledger_holder(led)
            if holder is not None:
                return holder
        # The server intentionally never returns a full device id in the
        # account list.  If the ledger has no hour yet, matching the six-char
        # prefix against this account's list is the only fallback available.
        if owned is not None:
            return vid[:6] in owned
        return None

    async def fresh_unclaimed_verifier(self) -> str:
        """Generate a new verifier id and validate that it is unowned."""
        since = self._local_midnight_utc_hour()
        last = ""
        for attempt in range(1, 21):
            last = new_verifier_id()
            try:
                r = await asyncio.wait_for(self._io(self.remote.points, last, since), timeout=10)
            except Exception:
                r = None
            if isinstance(r, dict) and r.get("ok") and not r.get("claimed"):
                return last
            if r is None and attempt >= 3:
                self.on_event("verifiers", f"新 verifier 暂时无法校验，保留 {last[:10]}")
                return last
            await asyncio.sleep(0.2)
        raise RuntimeError("could not generate an unclaimed verifier")

    async def _validate_one_verifier(self, vid: str,
                                     owned: set[str] | None,
                                     since: str) -> str | None:
        state = await self._verifier_ownership(vid, owned, since)
        if state is True:
            return None
        if state is None:
            self.on_event("verifiers", f"归属未知，保留 {vid[:10]}")
            return None
        new = await self.fresh_unclaimed_verifier()
        self.on_event("verifiers", f"{vid[:10]} 不属于当前账号，替换为 {new[:10]}")
        return new

    def _apply_verifier_replacements(self, ids: list[str],
                                     replace: dict[str, str]) -> None:
        self.cfg["verifiers"] = [replace.get(v, v) for v in ids]
        claimed = [v for v in (self.cfg.get("claimed") or []) if v not in replace]
        self.cfg["claimed"] = claimed
        self.ensure_verifiers()
        try:
            self.on_config_changed()
        except Exception:
            pass

    async def validate_verifier_ownership(self) -> None:
        """Replace verifiers held by another account before work starts."""
        if not self.remote.cookie or not self.account_signed_in:
            self.on_event("verifiers", "归属校验跳过：当前未登录")
            return
        owned = await self._account_device_prefixes()
        since = self._local_midnight_utc_hour()
        ids = list(self.cfg.get("verifiers") or [])
        replace: dict[str, str] = {}
        for vid in ids:
            new = await self._validate_one_verifier(vid, owned, since)
            if new:
                replace[vid] = new
        if replace:
            self._apply_verifier_replacements(ids, replace)

    def ensure_verifiers(self) -> None:
        want = self.cfg["max_verifiers"]
        ids = list(self.cfg.get("verifiers") or [])
        while len(ids) < want:
            ids.append(new_verifier_id())
        self.cfg["verifiers"] = ids[:want]
        claimed = set(self.cfg.get("claimed") or [])
        self.verifiers = [
            Verifier(v, label=f"v{i+1:02d}", account_claimed=(v in claimed))
            for i, v in enumerate(self.cfg["verifiers"])
        ]

    async def _account_probe(self) -> None:
        """Confirm the session with the light endpoint when points fail."""
        if not self.remote.cookie:
            return
        if time.time() - self.account_info_at < 300:
            return
        info = await self._io(self.remote.account_info)
        self.account_info_at = time.time()
        if isinstance(info, dict) and info.get("ok"):
            self.account_signed_in = True
        elif isinstance(info, dict) and info.get("http_error") in (401, 403):
            self.remote.cookie = None
            self.account_signed_in = False

    def _reset_for_start(self) -> None:
        """A restart after an engine update must not append a second set."""
        if self.running:
            return
        self.engines.clear()
        self.slots.clear()
        self.slot_task.clear()
        self.slot_tasks.clear()
        self.slot_supervisors.clear()
        self.slot_idle_count.clear()
        self.slot_restarting.clear()
        self.fetching_count = 0
        self.riven_slots.clear()
        self.drained_lanes = 0
        self.stopped_slots.clear()
        self.slot_cpu_sample.clear()
        self.tasks.clear()
        self.riven_sending.clear()
        self.maintenance = False
        # A restart is a NEW RUN: force the first points read instead of
        # inheriting the previous object's `account_at` and skipping the
        # authoritative /api/account/devices pass.
        self.account_at = 0.0
        self.account_info_at = 0.0
        self.account_points = None
        self.account_signed_in = None

    async def _login_account(self) -> None:
        acct = self.cfg.get("account") or {}
        if not acct.get("email"):
            return
        r = {}
        for attempt in range(1, 4):
            r = await self._io(self.remote.login, acct["email"], acct["password"]) or {}
            signed_in = r.get("ok") and (
                r.get("outcome") == "signed_in" or r.get("outcome") is None)
            if signed_in:
                break
            if r.get("http_error") in (400, 401, 403):
                break
            if attempt < 3:
                await asyncio.sleep(2.0 * attempt)
        self.account_signed_in = bool(
            r.get("ok") and (r.get("outcome") == "signed_in" or r.get("outcome") is None))
        self.account_info_at = time.time()
        self.on_event("login", f"login -> {r.get('outcome') or r}")

    def _apply_engine_ids(self, rel: dict) -> None:
        # THE ON-DISK ID IS NOT THE WIRE ID.  `engine_id` is sent to the server
        # and must describe the core we are actually running.  `local_engine`
        # comes from the last successful install state and is the only value
        # the update checker may compare with release.json.
        try:
            import sync_engine
            local_state = sync_engine.load_state()
        except Exception:
            local_state = {}
        self.local_engine = (local_state.get("engine")
                             or self.cfg.get("known_engine") or None)
        self.served_engine = (rel.get("engine") or self.local_engine
                              or self.cfg.get("known_engine") or None)
        # `wire_engine` is the PIN the settings page edits.  `engine_id` is what
        # the server is actually told, and `_effective_engine` decides between
        # them: a pin is honoured only while the installed core is older than
        # the served one.
        configured_wire = str(self.cfg.get("wire_engine") or "").strip()
        self.wire_engine = configured_wire or self.local_engine or self.served_engine
        self.engine_id = self._effective_engine()
        self.release = rel.get("release")
        # Protocol negotiation rides along: release.json and every hand-out
        # reply may carry `protocol` / `protocols_served` (protocol 8).
        if isinstance(rel, dict):
            self._learn_proto_served(rel)
        if rel.get("engine"):
            self.on_event("release", f"wire engine {self.engine_id} / served "
                                     f"{self.served_engine} release {self.release}")
        else:
            self.on_event("release", f"release.json unavailable "
                                     f"({rel.get('http_error') or rel.get('transport_error')})"
                                     f" — using known engine {self.engine_id}")
        if not self.engine_id:
            raise RuntimeError("no engine id: release.json unreachable and no known_engine in config")

    def _effective_engine(self, pin: str | None = None) -> str | None:
        """The engine id the server is told.

        A manual pin exists for a machine whose installed core really is older.
        When the installed core IS the served core, sending the pinned older id
        makes every answer a join-only witness: the server only credits it when
        a served-engine client leads the fact and the bits match, and drops it
        once a fact settles without it.  That is the high invalid ratio, so the
        pin is ignored in that case.
        """
        chosen = self.wire_engine if pin is None else pin
        if (self.local_engine and self.served_engine
                and self.local_engine == self.served_engine):
            return self.local_engine
        return chosen or self.local_engine or self.served_engine

    def _plan_max_lanes(self, n: int) -> list[int] | None:
        """Balanced P/E lanes, or None when the caller must fall back."""
        selected = self._engine_selected()
        groups, primary, note = self._core_plan(n, selected)
        if not (groups and all(groups)):
            self.slot_cpu_sets = []
            self.slot_primary_sets = []
            self.slot_primary = []
            self.slot_cpus = []
            return None
        self._store_core_plan(groups, primary)
        smt = cores.smt_info()
        smt_s = (f"SMT 开（每核 {smt['threads_per_core']} 线程，"
                 f"{smt['physical_cores']} 核 / {smt['logical_threads']} 线程）"
                 if smt["enabled"] else
                 f"SMT 关（{smt['physical_cores']} 核 / "
                 f"{smt['logical_threads']} 线程）")
        self.core_type_note = f"{smt_s}；{note}"
        return [max(1, len(g)) for g in groups]

    def slot_count(self) -> int:
        """HOW MANY COMPUTE PROCESSES run: config's `slots`, always 1..10."""
        return config.clamp_slots((getattr(self, "cfg", None) or {}).get("slots"))

    def _apply_lane_reserve(self, lanes: list[int]) -> list[int]:
        """Leave the client, the engine hosts and Windows a home CPU.

        WHY THIS EXISTS (measured 2026-10-16): 15 lanes on a 16-logical-core
        machine leaves NOTHING for the client's Python, the engine host threads,
        Windows and interrupts.  Demand measured 16.38 cores against 16, and a
        shortfall is paid out of every runnable thread, so the lanes sit at
        93-98% instead of 99%+ (97.7% with logging off, 93.2% with it full;
        other processes alone measured 1.21 cores = 7.6% of the machine).

        EVERY MATURE SCHEDULER RESERVES FOR THIS: tokio's CpuRuntime spawns
        `available_parallelism - reserve` workers; rayon's bounded spawners burn
        88% less CPU than unbounded ones; simfleet re-tunes its lane count every
        20 s; Slurm backfills around a held-back margin.  Here the reserve is a
        fixed budget, `lane_reserve` (default 2 = the measured client+OS
        footprint rounded up).

        The reserve comes off the MACHINE's logical cores, never off a pool the
        log-task already shrank, so the result cannot double-count.
        """
        # DEFAULT 0 = OFF.  Measured 2026-10-16 on this machine: 14 lanes gave
        # 97.0% mean duty (p5 93.8) against 15 lanes' 97.5% (p5 95.3) — reserving
        # a core does NOT buy occupancy, it only shortens the fold's tail.  The
        # knob stays for field A/B.
        try:
            reserve = int(self.cfg.get("lane_reserve", 0))
        except Exception:
            reserve = 0
        if reserve <= 0 or not lanes:
            return lanes
        try:
            machine = (len(cores.cpu_sets()) or len(cores.topology())
                       or os.cpu_count() or 0)
        except Exception:
            machine = 0
        total = sum(int(x) for x in lanes)
        avail = max(1, (machine or total) - reserve)
        if total <= avail:
            return lanes
        try:
            out = cores.distribute_threads(avail, len(lanes))
        except Exception:
            return lanes
        if sum(out) >= total:
            return lanes
        self.log.event("lane_reserve", before=total, after=sum(out),
                       reserve=reserve, machine=machine)
        return out

    def _plan_lanes(self) -> tuple[int, list[int]]:
        n = self.slot_count()
        lane_cfg = self.cfg.get("lanes_per_slot")
        ct = str(self.cfg.get("core_type") or "auto").lower()
        lanes = self._plan_max_lanes(n) if ct == "max" else None
        if lanes:
            lanes = self._apply_lane_reserve(lanes)
            self.slot_lanes = lanes
            self.cfg["lane_plan"] = list(lanes)
            return n, lanes
        if ct != "max":
            self.slot_cpu_sets = []
        if lane_cfg in (None, 0, "auto"):
            # The compute pool, not the machine: the client's log-task thread
            # is not a lane any engine may use.
            pool = self._engine_selected()
            total_threads = (len(pool) or len(cores.cpu_sets())
                             or len(cores.topology())
                             or os.cpu_count() or n)
            lanes = cores.distribute_threads(total_threads, n)
        else:
            # A single fixed number is still accepted for explicit setups.
            lanes = [max(1, int(lane_cfg)) for _ in range(n)]
        lanes = self._apply_lane_reserve(lanes)
        self.slot_lanes = lanes
        self.cfg["lane_plan"] = list(lanes)
        return n, lanes

    async def _start_engines(self, n: int) -> None:
        try:
            limit_pct = max(10, min(100, int(self.cfg.get("cpu_limit_percent") or 100)))
        except Exception:
            limit_pct = 100
        if self.cpu_limit is not None:
            self.cpu_limit.close()
        self.cpu_limit = cores.CpuRateLimit(limit_pct)
        if not self.cpu_limit.set_percent(limit_pct) and limit_pct < 100:
            self.on_event("cpu", f"CPU cap unavailable: {self.cpu_limit.error}")
        for i in range(n):
            self.engines.append(Engine(
                self.engine_exe(), self.cfg["engine_base_port"] + i,
                cpu_limit_percent=limit_pct, logger=self.log))
            self.slot_task[i] = None
            self.slots.append(SlotState(i, self.cfg["engine_base_port"] + i))
        # ALL AT ONCE: each one waits for its /api/meta, so in a row they add up.
        await asyncio.gather(*(self._cpu(e.start, self.cpu_limit) for e in self.engines))
        self.cpu_limit_actual = self.cpu_limit.actual_percent()
        if limit_pct < 100:
            for e in self.engines:
                if e.cpu_limit_error:
                    self.on_event("cpu", f":{e.port} CPU 限速失败: {e.cpu_limit_error}")
        # CPU SETS MUST BE IN PLACE BEFORE THE ENGINE'S WORKERS ARE BORN.
        self.apply_core_type()
        self.on_event("engines", f"{n} engine processes up on "
                                 f"{self.cfg['engine_base_port']}..{self.cfg['engine_base_port']+n-1}")

    def _log_session_start(self, n: int) -> None:
        plan = self._log_cpu()
        self.on_event("update",
                      "工作模式：只做榜单（常驻队列，收到紫卡即退回）"
                      if self._board_only()
                      else "工作模式：榜单 + 紫卡（每个 ver 一个任务，"
                           "紫卡算到完成为止）")
        if plan is not None:
            self.log.event("log_cpu_pinned", logical=plan["logical"],
                           set_id=plan["set_id"], ht=plan["ht"],
                           siblings=plan["siblings"],
                           compute_lanes=len(plan["compute_ids"]),
                           reason=plan["reason"])
            self.on_event("cores", f"客户端线程 -> 第 {plan['logical']} 号"
                                   f"逻辑处理器（{plan['reason']}）")
        self.log.event("session_start", engine_id=self.engine_id,
                       release=self.release, engine_exe=self.engine_exe(), slots=n,
                       lanes_per_slot=list(getattr(self, "slot_lanes", [])),
                       work_lanes=int(self.cfg.get("work_lanes") or 16),
                       single_verifier_queue=bool(self.cfg.get("single_verifier_queue")),
                       board_only=self._board_only(),
                       forced_queue=bool(self.cfg.get("queue_fallback")),
                       log_cpu=(plan["logical"] if plan is not None else None),
                       config={k: v for k, v in self.cfg.items() if k != "account"},
                       account=(self.cfg.get("account") or {}).get("email"))

    def _spawn_loops(self, n: int) -> None:
        self.running = True
        self._stopped = False
        self.apply_cores()
        for i in range(n):
            task = asyncio.create_task(self._slot_supervisor(i))
            self.slot_supervisors[i] = task
            self.slot_tasks.append(task)
        self.slot_tasks.append(asyncio.create_task(self.points_loop()))
        self.slot_tasks.append(asyncio.create_task(self.task_status_loop()))
        self.slot_tasks.append(asyncio.create_task(self.leaderboard_loop()))
        self.slot_tasks.append(asyncio.create_task(self._slot_watchdog()))
        self.slot_tasks.append(asyncio.create_task(self.verifier_watchdog_loop()))
        self.slot_tasks.append(asyncio.create_task(self.heartbeat_loop()))
        self.slot_tasks.append(asyncio.create_task(self.expire_loop()))
        self.slot_tasks.append(asyncio.create_task(self.pending_riven_loop()))
        self.slot_tasks.append(asyncio.create_task(self._slot_stall_loop()))
        self.slot_tasks.append(asyncio.create_task(self.sync_loop()))

    async def start(self) -> None:
        self._reset_for_start()
        # FIRST, ALWAYS: without verifiers the whole loop has nothing to do.
        self.ensure_verifiers()
        await self._login_account()
        await self.validate_verifier_ownership()
        rel = await self._io(self.remote.release_info)
        self._apply_engine_ids(rel)
        # DELAYED CLAIM.  Work verifiers start unclaimed so they can act as
        # independent witnesses.
        already = sum(1 for v in self.verifiers if v.account_claimed)
        self.on_event("claim", f"delayed claim: {already} already claimed, "
                              f"{len(self.verifiers)-already} unclaimed")
        n, lanes = self._plan_lanes()
        self.on_event("engines", f"starting {n} engine process(es), lanes {lanes}…")
        await self._start_engines(n)
        self._log_session_start(n)
        self._spawn_loops(n)

    async def _release_one_for_shutdown(self, v: Verifier) -> int:
        had = (1 if v.lease else 0) + (1 if v.queued else 0)
        await self._give_back_all(v, "shutdown", SHUTDOWN_RELEASE_S)
        return had if (not v.lease and not v.queued) else 0

    async def _cancel_slot_tasks(self) -> None:
        for t in self.slot_tasks:
            t.cancel()
        if not self.slot_tasks:
            return
        await asyncio.gather(*self.slot_tasks, return_exceptions=True)
        self.slot_tasks = []
        self.slot_supervisors.clear()
        self.slot_idle_count.clear()

    async def _wait_inflight_tasks(self) -> None:
        pending = [t for t in self.tasks if not t.done()]
        if not pending:
            return
        self.on_event("stop", f"waiting for {len(pending)} in-flight task(s)…")
        _done, still = await asyncio.wait(pending, timeout=10)
        for t in still:
            t.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    async def _release_held_leases(self) -> int:
        # GIVE IT ALL BACK AT ONCE, AND WITH A SHORT LEASH.  One at a time,
        # sixteen verifiers holding two works each is up to thirty-two round
        # trips, and `Remote` waits two minutes for each of them by default.
        held = [v for v in self.verifiers if v.lease or v.queued]
        if not held:
            return 0
        counts = await asyncio.gather(
            *(self._release_one_for_shutdown(v) for v in held),
            return_exceptions=True)
        return sum(c for c in counts if isinstance(c, int))

    async def _stop_engines_and_limit(self) -> None:
        # The stop is synchronous and waits for the process: run it off the
        # loop, or the console cannot repaint while three of them exit.
        if self.engines:
            await asyncio.gather(*(self._stop_engine(i)
                                   for i in range(len(self.engines))),
                                 return_exceptions=True)
        if self.cpu_limit is not None:
            self.cpu_limit.close()
            self.cpu_limit = None

    async def stop(self) -> None:
        """Stop taking work, WAIT FOR WHAT IS IN FLIGHT, then let go of the
        leases and the engines — in that order.

        Releasing first (and killing the engines under a running `_compute`)
        left a task that had already been given its lease back, and its upload
        then landed on an IDLE verifier."""
        if self._stopped:
            return
        self._stopped = True
        self.running = False
        await self._cancel_slot_tasks()
        await self._wait_inflight_tasks()
        # SEND WHAT IS FINISHED AND UNSENT before the leases go back: quitting is
        # not a reason to lose a computed answer, and the release below would
        # make it untakeable.  Bounded — quitting must still quit.
        try:
            await asyncio.wait_for(self._flush_pending_riven(), timeout=5.0)
        except Exception:
            pass
        await self._wait_riven_sends(timeout=10.0)
        # Protocol 8: hand in kept board answers and release server-side
        # first — quitting must still quit, so this is bounded; whatever is
        # left falls through to the ordinary per-lease release below.
        try:
            await asyncio.wait_for(self._sync_final_round(False), timeout=120.0)
        except Exception:
            pass
        self.returned_on_stop = await self._release_held_leases()
        await self._stop_engines_and_limit()
        counts = self.task_status.counts_of([v.vid for v in self.verifiers])
        final_stats = dict(self.stats)
        final_stats["invalid"] = counts["invalid"]
        final_stats["returned"] = counts["returned"]
        self.log.event("session_stop", stats=final_stats, maintenance=self.maintenance)
        self.tasks.clear()
        self.on_event("stop", "engines down, leases given back")

    def _stale_leases(self) -> list[Verifier]:
        """Verifiers whose work is NOT on an engine any more.

        A verifier can sit in COMPUTING with nothing running it: the engine
        finished its row, or the lease lapsed and the picker stopped choosing
        it.  Anything that waits on the STATE alone then waits for ever — which
        is exactly what held a version change up for the whole 32-minute
        timeout, with the console showing three idle engines and one verifier
        that said 计算中.
        """
        out = []
        for v in self.verifiers:
            if v.state is not State.COMPUTING:
                continue
            works = [w for w, _ in v.works() if w.is_live()]
            if not works:
                continue
            if not any(self._slot_for_work(v, w) is not None for w in works):
                out.append(v)
        return out

    async def _wait_fetches_finish(self, deadline: float) -> None:
        while time.time() < deadline:
            if not any(v.state is State.FETCHING for v in self.verifiers):
                return
            await asyncio.sleep(0.5)

    async def _return_unstarted(self) -> None:
        for v in list(self.verifiers):
            if v.queued is not None:
                await self._give_back_queued(v, "engine update")
        for v in list(self.verifiers):
            if v.state is State.HOLDING:
                await self._give_back_all(v, "engine update")
        for v in self._stale_leases():
            self.on_event("update", f"{v.label} 没有引擎在算，先归还")
            await self._give_back_all(v, "engine update")

    def _drain_active(self) -> bool:
        computing = any(self.slot_task.values())
        uploading = any(st.uploading for st in self.slots)
        pending = [t for t in self.tasks if not t.done()]
        return computing or uploading or bool(pending)

    async def _wait_engines_finish(self, deadline: float,
                                   timeout: float) -> None:
        while time.time() < deadline:
            if not self._drain_active():
                return
            await asyncio.sleep(0.5)
        self.on_event("update",
                      f"drain timed out after {timeout:.0f}s; giving back remaining leases")

    async def _return_remaining(self) -> None:
        for v in list(self.verifiers):
            if v.lease or v.queued:
                await self._give_back_all(v, "engine update")

    async def drain_for_update(self, timeout: float = 1900.0) -> bool:
        """Stop fetching, let accepted work finish, then stop the engines.

        This is deliberately separate from ``stop``: the caller wants the
        engines running while the already accepted jobs finish, and only then
        wants the exe to become replaceable.
        """
        self.maintenance = True
        deadline = time.time() + timeout
        self.on_event("update", "maintenance: fetching stopped")
        await self._wait_fetches_finish(deadline)
        await self._return_unstarted()
        await self._wait_engines_finish(deadline, timeout)
        await self._return_remaining()
        await self.stop()
        return not any(v.lease or v.queued for v in self.verifiers)

    # ------------------------------------------------------------- the rules
    def active_count(self) -> int:
        return int(self.cfg["active_verifiers"])

    def pool(self) -> list[Verifier]:
        """Verifiers with at least one pending task in their current work."""
        return [v for v in self.verifiers if v.available_tasks()]

    def _count_tasks(self, kind: str) -> int:
        """Pending rows of that kind across BOTH works a verifier may hold."""
        total = 0
        for v in self.verifiers:
            for work, _ in v.works():
                if work.kind == kind:
                    total += len(work.pending_tasks())
        return total

    def pool_riven(self) -> int:
        """Every unassigned riven task, current pack/task and ahead work."""
        return self._count_tasks("riven_gain")

    def pool_board(self) -> int:
        """Every unassigned board row, current pack and ahead pack."""
        return self._count_tasks("board")

    def computing_count(self) -> int:
        return sum(len(w.inflight_tasks())
                   for v in self.verifiers for w, _ in v.works())

    def queued_count(self) -> int:
        return sum(len(v.queued.pending_tasks()) + len(v.queued.inflight_tasks())
                   for v in self.verifiers if v.queued)

    def update_idle(self) -> bool:
        """True only when no engine has work and no lease is still held.

        This is the safety gate for replacing wfsim-core.exe.  Checking only
        `paused` is not enough: pause returns immediately and lets an in-flight
        row finish; the exe must not be touched until that row and its upload
        are both gone.
        """
        if any(self.slot_task.values()):
            return False
        if any(st.uploading for st in self.slots):
            return False
        if any(v.lease or v.queued for v in self.verifiers):
            return False
        if any(not t.done() for t in self.tasks):
            return False
        return True

    def holding_count(self) -> int:
        return sum(len(w.pending_tasks())
                   for v in self.verifiers for w, _ in v.works())

    def total_tasks(self) -> int:
        """Everything the fleet owns: current work + ahead work."""
        current = sum(len(v.lease.tasks) for v in self.verifiers if v.lease)
        queued = sum(len(v.queued.tasks) for v in self.verifiers if v.queued)
        return current + queued

    def max_tasks(self) -> int:
        """An estimate of row capacity, not work capacity.

        The server holds at most two works per verifier, but a board work is a
        pack with a pace-sized number of rows.  Use the live average pack size
        so the TUI's 40/40 style readout stays meaningful.
        """
        # "2 with the optional ahead queue, 1 without" — a plain verifier holds
        # one work; the ahead queue is the second one.
        works_per_verifier = 2 if self.cfg.get("single_verifier_queue") else 1
        works = int(self.cfg.get("max_verifiers") or 0) * works_per_verifier
        sizes = [len(v.lease.tasks) for v in self.verifiers if v.lease]
        sizes += [len(v.queued.tasks) for v in self.verifiers if v.queued]
        avg = max(1, round(sum(sizes) / len(sizes))) if sizes else 1
        return max(self.total_tasks(), works * avg)

    def queued_tasks(self) -> list[dict]:
        out = []
        for v in self.verifiers:
            q = v.queued
            if q is None:
                continue
            for task in q.tasks:
                out.append({
                    "verifier": v.vid,
                    "lease": q.lease_id,
                    "kind": task.kind,
                    "label": task.label(),
                    "question": task.question,
                    "index": task.index,
                    "total": len(q.tasks),
                    "forced": task.manual_priority,
                    "source": q.source,
                    "left_s": round(q.seconds_left()),
                })
        return out

    def fetch_limit(self) -> int:
        """One fetch at a time whenever any riven exists; otherwise the cap."""
        if self.riven_total() > 0:
            return 1
        try:
            return max(1, int(self.cfg.get("fetch_parallel") or 2))
        except Exception:
            return 2

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key) or default)
        except Exception:
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key) or default)
        except Exception:
            return default

    def _board_sync_healthy_fleet(self) -> bool:
        """Fleet-wide protocol-8 health: every verifier is syncing fine.

        ONE unhealthy verifier re-opens the protocol-7 road for the whole
        fleet: a sync that cannot renew cannot teach `fallback: 7` either (that
        needs a SUCCESSFUL sync), so without this the fleet sits idle while
        its leases lapse under compute.
        """
        if not self._board_sync_active():
            return True
        for v in self.verifiers:
            if v.vid is None:
                continue
            if not self._sync_verifier_healthy(v):
                return False
        return True

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _slot_for_work(self, v: Verifier, work) -> SlotState | None:
        """Find the engine that owns ONE of v's works, it or its upload."""
        if work is None:
            return None
        for st in self.slots:
            if st.verifier == v.vid and st.lease == work.lease_id:
                return st
            # The compute loop clears `st.verifier` before the answer upload
            # completes.  A late `ahead:true` response must still be attached
            # to that same engine so its next task is not lost from the UI.
            if (st.uploading == v.vid
                    and st.uploading_lease == work.lease_id):
                return st
        return None

    def _slot_for_verifier(self, v: Verifier) -> SlotState | None:
        """The engine on v's primary work, if a slot has it."""
        return self._slot_for_work(v, v.lease)

    def _slot_next_fields(self, st: SlotState, v: Verifier, q) -> None:
        if q is None or (st.verifier == v.vid and st.lease == q.lease_id):
            st.next_verifier = None
            st.next_kind = None
            st.next_lease = None
            st.next_label = ""
            st.next_deadline = 0.0
            return
        st.next_verifier = v.vid
        st.next_kind = q.kind
        st.next_lease = q.lease_id
        st.next_label = q.label()
        st.next_deadline = q.deadline

    def _refresh_slot_next(self, v: Verifier) -> None:
        """Show the verifier's OTHER work as 'queued' on its running slots.

        Once the ahead work is itself on an engine it is no longer 'queued':
        both slots show their own work, and neither shows a duplicate next row.
        """
        q = v.queued
        if q is not None and any(st.verifier == v.vid and st.lease == q.lease_id
                                 for st in self.slots):
            q = None
        for st in self.slots:
            if st.verifier == v.vid or st.uploading == v.vid:
                self._slot_next_fields(st, v, q)

    def record_slot_next(self, v: Verifier, lease) -> SlotState | None:
        """Record the ahead:true lease on the engine that will run it next."""
        self._refresh_slot_next(v)
        st = self._slot_for_verifier(v)
        self.log.event("queue_recorded", slot=(st.slot if st else None),
                       verifier=v.vid, lease=lease.lease_id,
                       work_kind=lease.kind, source=lease.source,
                       deadline=lease.deadline)
        return st

    def clear_slot_next(self, v: Verifier, lease=None) -> None:
        """Clear the dispatcher's second-task record after release/expiry."""
        self._refresh_slot_next(v)

    async def _send_claim(self, v: Verifier, label: str) -> dict:
        try:
            return await self._io(self.remote.claim, v.vid, label)
        except Exception as e:
            return {"transport_error": f"{type(e).__name__}: {e}"}

    def _apply_claim_result(self, v: Verifier, r: dict, ok: bool) -> None:
        if not ok:
            v.last_error = str(r.get("error") or r.get("body") or r)
            self.on_event("claim", f"{v.label} delayed claim failed: {v.last_error}")
            return
        v.account_claimed = True
        # A device's lifetime total now includes work done before the account
        # owned it.  Start this run's baseline at the claim, not at the older
        # unclaimed reading.
        v.points_known = False
        v.base_confirmed = None
        claimed = self.cfg.setdefault("claimed", [])
        if v.vid not in claimed:
            claimed.append(v.vid)
        try:
            self.on_config_changed()
        except Exception as e:
            self.on_event("error", f"could not persist claim: {e}")
        self.on_event("claim", f"{v.label} claimed after current+ahead "
                               f"(two tasks held)")

    async def _maybe_claim(self, v: Verifier) -> bool:
        """Delayed claim: only after the first and `ahead:true` tasks are held.

        The server can hand work to an unclaimed verifier; claiming is account
        bookkeeping.  Doing it only after both leases are present is what makes
        a copy of the client usable without immediately stealing the device
        identity on the original machine.
        """
        if v.account_claimed or v.claim_pending:
            return v.account_claimed
        if v.lease is None or v.queued is None:
            return False
        v.claim_pending = True
        label = claim_label(v)
        self.log.event("claim_begin", verifier=v.vid, label=label,
                       tasks_held=2, current_lease=v.lease.lease_id,
                       queued_lease=v.queued.lease_id,
                       current_source=v.lease.source,
                       queued_source=v.queued.source)
        try:
            r = await self._send_claim(v, label)
        finally:
            v.claim_pending = False
        ok = bool(isinstance(r, dict) and r.get("ok"))
        self.log.event("claim_result", verifier=v.vid, ok=ok, response=r,
                       tasks_held=2)
        self._apply_claim_result(v, r, ok)
        return ok

    def engine_exe(self) -> str:
        """ABSOLUTE OR RELATIVE TO THIS FOLDER. A portable copy names the engine
        `engine\\wfsim-core.exe`, so the whole folder can be moved anywhere.

        ONE EXCEPTION, the local line: when this machine has built its own core
        for exactly the revision the client is on (`sync_engine.native_active`),
        that overlay is what runs.  `wfsim-core.exe` stays on disk untouched, so
        an overlay that stops matching is simply ignored and the delivered core
        takes over again -- the fallback cannot be lost to a local build.
        """
        exe = str(self.cfg.get("engine_exe") or "")
        if os.path.isabs(exe):
            return exe
        try:
            import sync_engine
            over = sync_engine.engine_exe_override(exe)
        except Exception:                                      # noqa: BLE001
            over = None
        if over:
            return over
        return os.path.join(paths.ROOT, exe)

    def idle_slots(self) -> list[int]:
        return [s for s, v in self.slot_task.items() if v is None]

    def note_fetch(self, kind: str | None) -> None:
        """Verifier count is fixed; fetching is controlled by riven_total()."""
        return

    def _ahead_riven_ready(self, v: Verifier, work) -> bool:
        if v.queued is not None:
            return False
        elapsed = max(0.0, (time.time() - work.started_at)
                      if work.started_at else 0.0)
        ref_ms = self.last_riven_ms or (
            self._cfg_float("riven_first_ahead_s", 120.0) * 1000.0)
        return elapsed >= self._cfg_float("riven_ahead_at", 0.8) * (ref_ms / 1000.0)

    def _renew_allowed(self, v: Verifier, work) -> bool:
        """Queued (ahead) work is renewed only with the forced queue on.

        With `queue_fallback=0` an IDLE ahead pack is allowed to lapse: the
        client does not keep leasing rows it is not committed to computing.
        The primary work is always renewed, in both modes.

        A WORK WITH A ROW ON AN ENGINE IS THE OTHER EXCEPTION: `pick_for_slot`
        takes from both roles, so an ahead work can be computing long before
        it is promoted.  Letting its lease lapse under the compute throws the
        spent work away at the first heartbeat after promotion.  Anything
        already reserved keeps its lease whatever the role or the queue
        switch — `pause_drain` uses the same rule to decide what to keep.

        A FINISHED, UNDELIVERED ANSWER IS THE EXCEPTION: its result is already
        spent, so the lease that lets the re-send land is renewed whatever the
        role or the queue switch.
        """
        if self._riven_pending_exists(work.lease_id):
            return True
        if work.inflight_tasks():
            return True
        if getattr(work, "held_extra", False):
            return True
        if v.work_role(work) == "ahead" and not self._queue_fallback():
            return False
        return True

    def _board_only(self, v: Verifier | None = None) -> bool:
        """The forced-queue switch, read as THE MODE: 1 = board only, 0 = both.

        Spares follow the SAME switch as residents — they used to be exempt
        ("a spare takes whatever the pool has"), and that exemption is exactly
        how both spares each caught a riven they would never compute: the
        residents in board-only hand every riven back, the server hands the
        backed-up rivens to the only devices that keep them, `pick_for_slot`
        ranks board first so the riven waits behind a continuous supply, and
        `_riven_work_alone` then rests the spare's sync — a spare spending
        itself renewing a riven that never starts.  A spare's job is BOARD
        COVERAGE; a riven it cannot promptly compute is pure liability.

        `queue_fallback` is the old强制队列键, reused rather than duplicated.
        Under protocol 8 it had no effect any more (it only drove the retired
        classic `ahead` door), so giving it this meaning changes nothing that
        was working — and it is exactly the switch the user asked for.

        Board-only keeps a pack queued at all times — the server never hands a
        question someone waits on to a computer that still holds a row
        (worker/sync.js `holds.questions > 0` → `idle`) — and gives back any
        riven that arrives anyway.  Both halves are needed: the queue is what
        keeps rivens away, the give-back covers the pool-is-empty window.
        """
        try:
            return int(self.cfg.get("queue_fallback") or 0) == 1
        except (TypeError, ValueError):
            return False

    def _queue_fallback(self) -> bool:
        """The 1/0 switch: hand in nothing until the next work is already held.

        This forces the normal single-slot prefetch even when the optional
        timing-based queue is disabled.  The server decides whether that next
        work is a board pack or a riven.  A riven is accepted and promoted like
        any other task, never released merely because it arrived ahead.
        """
        try:
            return int(self.cfg.get("queue_fallback") or 0) == 1
        except Exception:
            return False

    def _ahead_board_ready(self, work) -> bool:
        rows = len(work.tasks)
        if rows <= self._cfg_int("board_small_pack_rows", 8):
            return True
        done = sum(1 for t in work.tasks if t.state == "done")
        need = int(rows * self._cfg_float("board_ahead_at", 0.8))
        return done >= need

    def _clear_forced_task(self) -> None:
        """Forget the one user-selected task and clear every marker."""
        for v in self.verifiers:
            for work in (v.lease, v.queued):
                if work is None:
                    continue
                for task in work.tasks:
                    task.manual_priority = False
        self.forced_task = None

    def _clear_forced_if(self, v: Verifier, work: Work | None) -> None:
        if (work is not None and self.forced_task is not None
                and self.forced_task[0] == v.vid
                and self.forced_task[1] == work.lease_id):
            self._clear_forced_task()

    def toggle_forced_task(self, vid: str, source: str) -> dict | None:
        """Set/clear the highest-priority pending task for one verifier row.

        `source` is "active" for the status column or "queued" for the prefetch
        column.  Only one task is remembered at a time: selecting another row
        clears the previous marker.
        """
        v = next((x for x in self.verifiers if x.vid == vid), None)
        if v is None:
            return None
        work = v.lease if source == "active" else v.queued
        if work is None:
            return None
        pending = work.pending_tasks()
        if not pending:
            return None
        # A pack can hold several rows; the top of the same order the scheduler
        # would use is the natural target of one row click.
        task = min(pending, key=functools.partial(_task_due, default=work.deadline))
        key = (v.vid, work.lease_id, task.index)
        if self.forced_task == key:
            self._clear_forced_task()
            return {"action": "clear", "verifier": v.vid,
                    "label": task.label(), "question": task.question}
        self._clear_forced_task()
        task.manual_priority = True
        self.forced_task = key
        return {"action": "set", "verifier": v.vid,
                "label": task.label(), "question": task.question}

    def _stale_start_limit(self) -> float:
        """Seconds a row may wait unstarted before it outranks a fresh one.

        `0` turns the promotion off; a key that is missing or unusable is the
        forty minutes `STALE_START_S` names, so an older config file needs no
        edit and the number is changed in one place.
        """
        raw = (getattr(self, "cfg", None) or {}).get("stale_start_promote_seconds")
        if raw is None:
            return STALE_START_S
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return STALE_START_S

    def _is_stale_start(self, task: WorkTask, now: float, limit: float) -> bool:
        """Leased that long ago and still not started — the starvation case."""
        return bool(limit > 0 and task.claimed_at
                    and now - task.claimed_at >= limit)

    def pick_for_slot(self, slot: int) -> tuple[Verifier, WorkTask] | None:
        """THE FRONT LEVEL, then board, then FIRST RECEIVED.

        The front level holds two kinds of row on purpose: the one a reader
        pointed at (`manual_priority`) and one that has waited too long for an
        engine.  They are the SAME level, so the ordinary keys below settle
        between them, and whichever is left after one is taken is the next
        thing the fleet reaches for.  It changes only which already-leased task
        a local compute slot takes next; no server priority changes.

        The third key is `claimed_at` — WHEN THE ROW REACHED US — so same-kind
        rows run oldest first.  It used to be `row_until`, and that worked by
        accident: a pack's local window was arrival + five minutes and was
        never renewed, so smaller window meant older pack — incidental FIFO.
        `_touch_sync_held` now pushes every named row to
        `now + sync_lease_ms` on each successful sync, which made `row_until`
        a stamp of the LAST SYNC, not of arrival: same-verifier rows tie
        exactly, ties fall back to verifiers-list order (residents first,
        spares last), and the ordering churns with every sync beat — the
        spares' packs drifted to the back of the fleet for the whole
        `stale_start` span.  `claimed_at` is set once at creation on BOTH
        fetch paths and is exactly the "how long has this waited" clock its
        field comment promises; lease urgency needs no third key — the sync
        renewal and the watchdog own the lease, not this sort.
        """
        if self.maintenance:
            return None
        now = time.time()
        limit = self._stale_start_limit()
        candidates: list[tuple[int, int, float, Verifier, Work, WorkTask]] = []
        for v in self.verifiers:
            for work, _role in v.works():
                if not work.is_live(now) or work.pausing:
                    continue
                for task in work.pending_tasks():
                    front = 0 if task.manual_priority else 1
                    if self._is_stale_start(task, now, limit):
                        front = 0
                    rank = 0 if task.kind == "board" else 1
                    candidates.append(
                        (front, rank, task.claimed_at or work.deadline,
                         v, work, task))
        if not candidates:
            return None
        candidates.sort(key=operator.itemgetter(0, 1, 2))
        return candidates[0][3], candidates[0][4], candidates[0][5]

    # ---------------------------------------------------------- transitions
    def _manual_old_grace(self, r: dict) -> bool:
        """A manual wire id may be old while the installed core is current."""
        return (bool(r.get("accepted_until"))
                and bool(self.wire_engine)
                and self.wire_engine != self.served_engine
                and self.local_engine == self.served_engine)

    def _empty_ask_wait(self, v: Verifier) -> float:
        """Seconds to wait after an empty answer: 5, then 10 while it lasts."""
        v.empty_asks += 1
        return EMPTY_ASK_S if v.empty_asks == 1 else EMPTY_ASK_MAX_S

    def _fetch_banned_reason(self, r: dict) -> tuple[float, str]:
        return (r.get("until") or 0) / 1000.0, "refused by the server"

    def _fetch_stale_reason(self, r: dict) -> tuple[float, str]:
        # A manual wire id can be old while the installed binary is already
        # current.  Do not turn that into an update or stop fetching before
        # the GitHub accepted list is checked.
        manual_old = self._manual_old_grace(r)
        note = ("engine accepted/grace check" if manual_old
                else "engine or protocol is out of date")
        if manual_old:
            until = time.time() + 10.0
        else:
            self.maintenance = True
            until = 0.0
        self.on_event("engine_stale",
                      "server rejected protocol/engine; checking accepted range"
                      if manual_old else
                      "server rejected protocol/engine; draining before update")
        return until, note

    @staticmethod
    def _fetch_cooling_reason(v: Verifier, code) -> tuple[float, str]:
        v.backoff_s = min(300.0, (v.backoff_s * 2) if v.backoff_s else 5.0)
        return (time.time() + v.backoff_s,
                f"http {code or 'transport'} — cooling {v.backoff_s:.0f}s")

    def _apply_fetch_empty(self, v: Verifier, r: dict, ahead: bool) -> None:
        code = r.get("http_error")
        if r.get("banned"):
            until, note = self._fetch_banned_reason(r)
        elif r.get("stale"):
            until, note = self._fetch_stale_reason(r)
        elif (code in (403, 429) or (isinstance(code, int) and code >= 500)
              or r.get("transport_error")):
            until, note = self._fetch_cooling_reason(v, code)
        else:
            until, note = 0.0, ""
        self.log.event("fetch_empty", verifier=v.vid, ahead=ahead, response=r,
                       note=note, until=until)
        if ahead:
            if not until and self._queue_fallback():
                until = time.time() + 5.0
            v.banned_until = until or v.banned_until
            v.got_nothing_ahead(note)
        else:
            if not until:
                until = time.time() + self._empty_ask_wait(v)
            v.got_nothing(banned_until=until, note=note)
            self.note_fetch(None)

    async def _hold_extra_riven(self, v: Verifier, work, ahead: bool) -> bool:
        """Surplus riven: HOLD it, never return it for being extra.

        The pool cap gates the ASK (`should_fetch_ahead`), so anything that
        still arrives over cap is an in-flight straggler.  It is kept in the
        verifier's free slot — marked `held_extra` so its lease is renewed
        whatever the queue switch — and computed in turn like any other held
        work.  The cap gate then holds off further asks until the pool drains
        below max, when asking resumes one fetch at a time.

        Returns True only when there is nowhere to put it (both slots somehow
        occupied): an untracked lease can neither renew nor compute, so that
        one is released as before rather than leaked.
        """
        if v.lease is not None and v.queued is not None:
            out = await self._release_lease(v, work)
            ok = bool(isinstance(out, dict) and out.get("ok"))
            self.log.event("extra_riven_returned", verifier=v.vid, ahead=ahead,
                           lease=work.lease_id, work_code=work.code,
                           riven_total=self.riven_total(), accepted=ok,
                           response=out, reason="no room to hold")
            self.log.task(work.lease_id, verifier=v.vid, ahead=ahead,
                          work_input=work.raw,
                          release_reason="extra riven returned (no room)",
                          release_response=out)
            self.task_status.mark_returned(v.vid, work, "extra riven returned")
            self.note_returned()
            if ahead:
                v.got_nothing_ahead("extra riven returned")
            else:
                v.got_nothing(note="extra riven returned")
            self.on_event("release", f"{v.label} returned extra riven "
                                     f"({work.lease_id[:8]}) "
                                     f"{'ok' if ok else 'unconfirmed'}")
            return True
        work.held_extra = True
        self.log.event("extra_riven_held", verifier=v.vid, ahead=ahead,
                       lease=work.lease_id, work_code=work.code,
                       riven_total=self.riven_total())
        self.log.task(work.lease_id, verifier=v.vid, ahead=ahead,
                      work_input=work.raw, note="extra riven held")
        self.on_event("queue", f"{v.label} 多拿的紫卡先留着 "
                               f"({work.lease_id[:8]})，等人少了再算")
        return False

    async def _ensure_queue(self, v: Verifier, timeout: float = 15.0) -> bool:
        """QUEUE FALLBACK: WAIT for the sync that brings the next pack.

        Called before a verifier hands in its current work.  There is exactly
        one way work arrives — the sync reply's `pack` — so there is exactly one
        thing this can do: wait for the sync already due at `_sync_next`, the
        moment the server said this id may next be handed work.

        IT MUST NOT NUDGE THE SYNC EARLIER.  A request before that moment is
        answered `early: true` — results and renewals served, no work — so
        asking sooner is strictly worse than asking on time.  A timeout lets
        the hand-in proceed: a server with nothing owed must not block work
        already finished.

        The classic `/api/board/work` fetch, its fetch cap and the
        `_queue_priority` ordering that let a held-answer verifier exceed that
        cap are all gone with protocol 7.
        """
        # PAUSE OUTRANKS AHEAD.  A riven upload can finish after pause_drain has
        # already returned; without this gate it would create a fresh queued
        # lease no engine is allowed to start, leaving the update waiting on
        # work that can never drain.
        if self.paused or self.maintenance:
            return True
        if not self._queue_fallback() or v.queued is not None:
            return True
        if v.lease is None:
            return False
        deadline = time.time() + max(0.0, timeout)
        while v.queued is None and time.time() < deadline:
            await asyncio.sleep(0.05)
        return v.queued is not None


    # ------------------------------------------------- the hand-in gate
    #
    # WITH `queue_fallback: 1` AN ANSWER IS NOT HANDED IN WITHOUT A QUEUED PACK.
    # Handing it in leaves the verifier holding nothing, and the server hands a
    # question someone waits on (a riven) only to a client holding nothing —
    # which board-only mode refuses, paying `RIVEN_REFUSE_RETRY_S` (70 s) of
    # cool-down for work nobody asked for.  It is also what the forced queue
    # MEANT before protocol 8 retired the door it used to drive.
    #
    # The wait is bounded by the server's own horizon, never by a local guess:
    # `row_ceiling_ms` is when another client may take the row too, so holding
    # past it can only lose work already done.

    def _submit_hold_s(self) -> float:
        """Seconds a finished board answer may be held back.

        `min(config, the server's row_ceiling_ms)`: the configured hour is the
        deliberate choice, the server's ceiling is the line that must not be
        crossed.  A missing/0/unreadable key falls back to `BOARD_SUBMIT_HOLD_S`.
        """
        try:
            ms = int(self.cfg.get("board_submit_hold_ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        if ms <= 0:
            ms = int(BOARD_SUBMIT_HOLD_S * 1000)
        try:
            ceiling = int(self._sync_dials.get("row_ceiling_ms") or 0)
        except (TypeError, ValueError):
            ceiling = 0
        if ceiling > 0:
            ms = min(ms, ceiling)
        return ms / 1000.0

    def _has_unsent_board(self, v: Verifier) -> bool:
        for work, _role in v.works():
            if (work is not None and not work.aborted and work.kind == "board"
                    and self._board_unsent(work)):
                return True
        return False

    def _will_ask_again(self, v: Verifier) -> bool:
        """Will this id ever be handed another pack?

        FALSE for a RETIRING spare and for an id OFF DUTY (beyond the active
        count): `_want_ms_for` sends `want_ms: 0` for both, so nothing will ever
        arrive and no amount of waiting changes that.

        EVERYTHING THAT "WAITS FOR THE NEXT PACK" MUST ASK THIS FIRST:
          * `_queue_empty` — otherwise the console shows 等待队列 with a ticking
            countdown on a retiring spare that will never ask again (measured
            2026-10-16: "显示备胎一直在申请");
          * the ANCHOR — a retiring id that keeps one row back holds its pack
            open for the whole `board_submit_hold_ms` (one hour) because no
            queue is ever coming, and a row held that long is the one another
            holder settles first (measured: 15 `board_anchor_lost` in 1.5 h).
            An id that will not ask again cannot be handed a riven either, so
            the anchor protects nothing and costs a row.
        """
        if getattr(v, "retire_at", 0):
            return False
        return self._verifier_on_duty(v)

    def _queue_empty(self, v: Verifier) -> bool:
        """True while this id holds its pack and WILL ask for the next one.

        THE QUEUE COLUMN'S BUSINESS, NOT THE HAND-IN'S.  Answers go out as soon
        as they exist and the ask is paced by `next_ms`; the only thing left to
        show is whether a second pack is in hand — and, if not, how long until
        the next ask tries again.  A verifier with no pack at all is idle, not
        queue-empty, so this needs the lease as well; and one that will never
        ask again (`_will_ask_again`) is not "waiting" for anything, so it must
        not claim to be.
        """
        return (self._board_only(v) and v.queued is None and v.lease is not None
                and self._will_ask_again(v))

    def _residents_covered(self) -> bool:
        """Whether every compute slot is covered by a working RESIDENT verifier.

        The same test `_spare_evaluate` stands a spare down on: a resident
        counts while it holds a lease or a queued pack or has a fetch in
        flight, and while it is not sitting out a riven cool-down.
        """
        now = time.time()
        working = 0
        for v in self.verifiers[:self.active_count()]:
            if v.riven_cooldown_until > now:
                continue
            if v.lease is not None or v.queued is not None:
                working += 1
                continue
            task = self.fetch_tasks.get(v.vid)
            if task is not None and not task.done():
                working += 1
        return working >= self.slot_count()

    def _accept_work(self, v: Verifier, work, r: dict, ahead: bool) -> None:
        self.log.event("fetch_work", verifier=v.vid, ahead=ahead,
                       work_kind=work.kind, tasks=len(work.tasks),
                       lease=work.lease_id, work=work.raw)
        self.log.task(work.lease_id, fetch_response=r, work_input=work.raw,
                      verifier=v.vid, ahead=ahead, engine_id=self.engine_id,
                      protocol=7, pace_ms=self.pace_ms)
        v.backoff_s = 0.0
        v.empty_asks = 0
        if ahead:
            got = v.got_work_ahead(work)
            if got is None:
                # The sync door filled the ahead slot while this fetch was on
                # the wire.  GIVE THE PACK BACK instead of orphaning it: an
                # orphan holds its lease until it lapses and counts against
                # the server's packs_held, so it blocks the next pack too.
                self.log.event("queue_ahead_duplicate", verifier=v.vid,
                               lease=work.lease_id, tasks=len(work.tasks),
                               queued_lease=(v.queued.lease_id if v.queued
                                             else None))
                self._spawn(self._release_lease(v, work))
                self.on_event("fetch", f"{v.label} 预取包与 sync 包撞槽，"
                                       f"已归还 {work.lease_id[:8]}")
                return
            st = self.record_slot_next(v, got)
            self.log.event("queue_recorded", verifier=v.vid,
                           work_kind=got.kind, lease=got.lease_id,
                           tasks=len(got.tasks), source=got.source,
                           slot=(st.slot if st is not None else None))
            where = f" slot {st.slot}" if st is not None else ""
            self.on_event("fetch-ahead", f"{v.label} <= ahead {got.kind} "
                                         f"{len(got.tasks)} row(s){where}")
            return
        v.got_work(work)
        self.note_fetch(work.kind)
        self.on_event("fetch", f"{v.label} <- {work.kind} "
                               f"{len(work.tasks)} row(s) / {work.lease_id[:8]}")

    async def _release_lease(self, v: Verifier, lease, timeout: int | None = None):
        try:
            if lease.kind == "riven_gain":
                return await self._io(self.remote.appraise_release, lease.code,
                                      lease.lease_id, v.vid, timeout)
            return await self._io(self.remote.release_lease, lease.lease_id,
                                  v.vid, timeout)
        except Exception as e:
            return {"transport_error": f"{type(e).__name__}: {e}"}

    async def _release_work_now(self, v: Verifier, work, why: str,
                                timeout: int | None = None) -> dict:
        """Release ONE named work and clear it from the matching FSM slot."""
        out = await self._release_lease(v, work, timeout)
        self.log.task(work.lease_id, release_reason=why, release_response=out)
        self._clear_forced_if(v, work)
        role = v.work_role(work)
        if role == "primary":
            v.give_back(why)
        elif role == "ahead":
            v.give_back_queued(why)
        self.task_status.mark_returned(v.vid, work, why)
        self.clear_slot_next(v, work)
        return out

    async def _cancel_riven_work(self, v: Verifier, work) -> None:
        """Stop the optimizer job of a named riven, if a slot is running it."""
        if work.kind != "riven_gain":
            return
        owner = self._slot_for_work(v, work)
        if owner is None:
            return
        try:
            await self._io(self.engines[owner.slot].post,
                           "/api/optimize/cancel", {}, 5, False)
        except Exception:
            pass

    async def _give_back(self, v: Verifier, why: str,
                         timeout: int | None = None) -> None:
        lease = v.lease
        if not lease:
            v.give_back(why); return
        await self._cancel_riven_work(v, lease)
        await self._release_work_now(v, lease, why, timeout)
        low = why.lower()
        if any(x in low for x in ("fail", "refus", "invalid", "compute")):
            self.note_invalid()
        else:
            self.note_returned()
        self.on_event("release", f"{v.label} gave back {lease.kind} ({why})")

    async def _give_back_queued(self, v: Verifier, why: str,
                                timeout: int | None = None) -> None:
        lease = v.queued
        if not lease:
            return
        if lease.inflight_tasks() and not lease.aborted:
            # The ahead work is on an engine too.  A riven is cancelled; a
            # board pack goes through its deadline path, which uploads the
            # rows it already finished and abandons the rest.
            if lease.kind == "board":
                await self._finalize_board_deadline(v, lease, restart_engine=True)
                if v.queued is not lease:
                    return
            else:
                await self._cancel_riven_work(v, lease)
        if v.queued is lease:
            await self._release_work_now(v, lease, why, timeout)
        self.note_returned()
        self.on_event("release", f"{v.label} gave back queued {lease.kind} ({why})")

    async def _give_back_all(self, v: Verifier, why: str,
                             timeout: int | None = None) -> None:
        if v.queued is not None:
            await self._give_back_queued(v, why, timeout)
        if v.lease is not None:
            await self._give_back(v, why, timeout)

    async def _restart_slot_after_abort(self, slot: int) -> None:
        """Stop a synchronous board fold that has already lost its lease.

        The board endpoint has no cancel call.  Terminating and restarting the
        one engine process is the only way to stop that fold immediately; the
        slot loop waits for this restart before taking another task.
        """
        port = self.engines[slot].port
        try:
            await self._cpu(self.engines[slot].stop)
            await self._cpu(self.engines[slot].start, self.cpu_limit)
            self.on_event("deadline", f"engine :{port} restarted; expired fold aborted")
        except Exception as e:
            self.on_event("error", f"engine :{port} restart failed: {e}")
        finally:
            self.slot_restarting.discard(slot)

    async def _give_back_work(self, v: Verifier, work, why: str,
                              flush_pending: bool = True) -> None:
        """Release only the named work, whichever role it now holds.

        A RIVEN ANSWER THAT IS FINISHED BUT UNSENT IS SENT FIRST: the release
        below is what makes it untakeable (`first: false`), and that is the one
        way to lose minutes of search already computed.
        """
        if flush_pending and getattr(work, "kind", "") == "riven_gain":
            await self._flush_pending_riven(work.lease_id)
            # Releasing while a copy is still on the wire is what turns a
            # recorded answer into `first:false`; wait a bounded moment for it.
            await self._wait_riven_sends(work.lease_id)
        role = v.work_role(work)
        if role == "primary":
            await self._give_back(v, why)
        elif role == "ahead":
            await self._give_back_queued(v, why)

    async def _renew_row(self, v: Verifier, work, task) -> bool:
        """One board-row heartbeat.  True while the row is still ours."""
        if not self._renew_allowed(v, work):
            return True
        sent = time.time()
        try:
            r = await self._io(self.remote.board_renew, work.lease_id, v.vid,
                               task.question)
        except Exception as e:
            task.renew_at = time.time() + RENEW_RETRY_S
            self.log.event("renew_error", verifier=v.vid, lease=work.lease_id,
                           question=task.question, attempt=task.renew_count,
                           error=f"{type(e).__name__}: {e}")
            return True                      # a miss is not a refusal
        if not isinstance(r, dict):
            task.renew_at = time.time() + RENEW_RETRY_S
            return True
        if r.get("http_error") == 404:
            # Older server: no heartbeat endpoint.  Fall back to the deadline
            # rules rather than trying (and failing) on every row.
            self.renew_supported = False
            self.on_event("deadline", "服务器没有 /api/board/renew，按旧租约规则")
            return True
        if not r.get("ok"):
            task.renew_at = time.time() + RENEW_RETRY_S
            self.log.event("renew_error", verifier=v.vid, lease=work.lease_id,
                           question=task.question, response=r)
            return True
        self.renew_supported = True
        if r.get("held") is False:
            task.lost = True
            self.task_status.mark_local_failure(v.vid, task, "lease_lost")
            self._drop_board_kept(work.lease_id, task.question, "row lost")
            self.log.task(work.lease_id, renew_lost_row=task.question)
            self.log.event("row_lost", verifier=v.vid, lease=work.lease_id,
                           question=task.question)
            self.on_event("deadline", f"{v.label} 一行已被其它机器拿走，放弃该行")
            return False
        try:
            ms = int(r.get("renew_ms") or 300_000)
        except Exception:
            ms = 300_000
        # The server does `lease_until = max(lease_until, now + renew_ms)`, so
        # take the max here as well: overwriting would let a renewal SHORTEN
        # the local view of a lease that is comfortably longer than one beat.
        task.row_until = max(task.row_until, sent + ms / 1000.0)
        task.renew_at = 0.0
        task.renew_count += 1
        return True

    async def _renew_riven(self, v: Verifier, work, now: float,
                           queued: bool = False) -> None:
        """Riven heartbeat: a fixed pulse.

        `queued=True` is the `ahead` lease: it is renewed exactly the same way,
        but if the server says it is gone it is the QUEUED work that goes back —
        giving back `v.lease` instead would throw away the riven being computed.
        """
        if not self._renew_allowed(v, work):
            return
        if not work.renew_at:
            # FIRST BEAT SHORT OF ONE PULSE, not immediately: firing at t=0
            # would only rewrite a comfortable number with a smaller one.  It
            # is also far enough inside the 120 s lease that the server cannot
            # have swept the row yet — a beat landing ON the expiry is what
            # used to come back `held:false` and cost the whole search.
            work.renew_at = now + RIVEN_RENEW_FIRST_S
            return
        if now < work.renew_at:
            return
        work.renew_at = now + RIVEN_RENEW_S
        sent = time.time()
        try:
            r = await self._io(self.remote.appraise_renew, work.code,
                               work.lease_id, v.vid)
        except Exception as e:
            work.renew_at = now + RENEW_RETRY_S
            self.log.event("renew_error", verifier=v.vid, lease=work.lease_id,
                           error=f"{type(e).__name__}: {e}")
            return
        if isinstance(r, dict) and r.get("http_error") == 404:
            self.renew_supported = False
            work.renew_at = now + RIVEN_RENEW_S
            self.on_event("deadline", "服务器没有紫卡 /renew，按旧租约规则")
            return
        if not (isinstance(r, dict) and r.get("ok")):
            work.renew_at = now + RENEW_RETRY_S
            return
        self.renew_supported = True
        if r.get("held") is False:
            self.log.event("row_lost", verifier=v.vid, lease=work.lease_id,
                           work_kind="riven_gain", queued=queued,
                           lease_age_s=self._work_age_s(work),
                           renews=int(getattr(work, "renew_count", 0) or 0),
                           pending_answer=self._riven_pending_exists(work.lease_id))
            self._note_riven_lost(now)
            if self._riven_pending_exists(work.lease_id):
                # THE LEASE ROW IS GONE BUT THE ANSWER MAY ALREADY BE IN.  The
                # server clears a question's lease the moment it takes an
                # answer (worker/tasks.js `recordMany`), so a renew landing
                # right after our own upload also reads `held:false`.  Wait for
                # any in-flight copy and let `appraise/mine` say which it is:
                # recorded closes as delivered, gone with no record lets go.
                # This branch runs BEFORE the delivered-race check below: a
                # kept answer must not be stripped just because its task row
                # already reads done — the retry loop owns that entry.
                self.on_event("deadline", f"{v.label} 紫卡租约已结束："
                                          f"正在确认已算出的答案是否已收到")
                await self._resolve_closed_lease(v, work)
                return
            if work.tasks and all(t.state == "done" for t in work.tasks):
                # RACE WITH OUR OWN UPLOAD, not a loss: the send already landed
                # (`first:true`, the kept entry was dropped on the way), so the
                # renew that followed it reads `held:false`.  Detach quietly —
                # no alarm, no release POST, no returned tally.
                self.log.event("riven_delivered_race", verifier=v.vid,
                               lease=work.lease_id, code=work.code)
                if v.work_role(work) is not None:
                    promoted = v.finish_work(work)
                    if promoted is not None:
                        self.log.event("queue_promoted", verifier=v.vid,
                                       lease=promoted.lease_id,
                                       work_kind=promoted.kind,
                                       source=promoted.source,
                                       via="renew delivered")
                    self.clear_slot_next(v, work)
                return
            if queued:
                self.on_event("deadline", f"{v.label} 预取的紫卡已被收走，退回队列项")
                await self._give_back_queued(v, "lease lost")
            else:
                self.on_event("deadline", f"{v.label} 紫卡租约已被收走，放弃计算")
                await self._give_back(v, "lease lost")
            return
        work.renew_count += 1
        # A KEPT ANSWER MOVES ITS OWN WINDOW: while the lease is renewed the
        # finished result stays in memory and keeps being re-sent.
        self._touch_pending_riven(work.lease_id, RIVEN_PENDING_KEEP_S)
        task = work.tasks[0]
        # `max` for the same reason as a board row above.  The length is the
        # server's own when it says one (`renew_ms`/`lease_ms`), the 120 s
        # `optimize` lease otherwise: the 300 s this used to assume is a
        # protocol-7 number, and over-stating it here is how a client goes on
        # computing a row the server already gave to somebody else.
        try:
            ms = int(r.get("renew_ms") or r.get("lease_ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        if ms <= 0:
            ms = int(RIVEN_LEASE_S * 1000)
        task.row_until = max(task.row_until, sent + ms / 1000.0)
        task.renew_count += 1

    def _collect_verifier_heartbeats(self, v: Verifier, now: float,
                                     due: list, rivens: list) -> None:
        for work, _role in v.works():
            if work is None or work.aborted:
                continue
            if (work.kind == "board" and self._sync_source(work)
                    and self._sync_verifier_healthy(v)):
                continue               # protocol 8 renews via sync `held`
            # PAUSED = NO NEW WORK, BUT WHAT IS ALREADY SPENT KEEPS ITS LEASE.
            # `pause_drain` hands back everything that has not started and
            # leaves a row that is on an engine to finish; that row's lease has
            # to go on being renewed, or it lapses under the compute and the
            # finished result can no longer be credited.  A finished, undelivered
            # riven answer is the other thing pause deliberately keeps.
            if self.paused and not (
                    self._riven_pending_exists(work.lease_id)
                    or work.inflight_tasks()):
                continue
            if not self._renew_allowed(v, work):
                continue
            if work.kind == "board":
                for t in work.live_tasks():
                    if (t.row_until and t.row_until - RENEW_LEAD_S <= now
                            and now >= t.renew_at):
                        due.append((t.row_until, v, work, t))
                continue
            if now >= work.renew_at:
                rivens.append((v, work, work is v.queued))

    def _collect_heartbeats(self, now: float):
        due: list = []          # (row_until, verifier, work, task)
        rivens: list = []       # (verifier, work, is_queued)
        for v in list(self.verifiers):
            self._collect_verifier_heartbeats(v, now, due, rivens)
        return due, rivens

    async def _renew_due_rows(self, due: list, rivens: list,
                              now: float) -> None:
        if due:
            due.sort(key=operator.itemgetter(0))
            batch = due[:RENEW_PER_PASS]
            await asyncio.gather(*(self._renew_row(v, w, t)
                                   for _, v, w, t in batch))
            self.log.event("renew", rows=len(batch), due=len(due),
                           riven=len(rivens))
        for v, work, queued in rivens:
            await self._renew_riven(v, work, now, queued)

    async def _heartbeats(self) -> None:
        """Keep EVERY live lease alive — the current one AND the queued one.

        A verifier holds two works under protocol 7: the one it is working on
        and the `ahead:true` one.  The queued pack is not being computed, which
        is exactly why it is easy to forget: its rows lapse unnoticed, `run()`
        hands the whole pack back, and the prefetch was wasted — the pool the
        ahead fetch was meant to build drains away.  Both are renewed here.

        Rows are renewed a few at a time ACROSS THE FLEET rather than per work,
        so sixteen verifiers holding two packs cannot fire a hundred requests in
        one pass.  The batch runs concurrently and is deliberately small: this
        is a network round trip on the loop that also dispatches work.
        """
        # PAUSE KEEPS RENEWING A FINISHED, UNDELIVERED ANSWER (see
        # `_collect_verifier_heartbeats`); every other work was handed back by
        # `pause_drain`.  MAINTENANCE still renews: a drain is finishing the row
        # on the engine, and stopping the beat would lapse the lease under it.
        now = time.time()
        due, rivens = self._collect_heartbeats(now)
        await self._renew_due_rows(due, rivens, now)

    def _deadline_restart(self, v: Verifier, work, restart_engine: bool) -> None:
        owner_slot = self._slot_for_work(v, work)
        if (restart_engine and owner_slot is not None
                and owner_slot.slot not in self.slot_restarting):
            self.slot_restarting.add(owner_slot.slot)
            self._spawn(self._restart_slot_after_abort(owner_slot.slot))

    @staticmethod
    def _deadline_rows(work):
        completed = [t for t in work.tasks
                     if t.state == "done" and t.result is not None
                     and not t.uploaded]
        unfinished = [t for t in work.tasks if t.state != "done"]
        for t in unfinished:
            t.state = "aborted"
        return completed, unfinished

    def _accept_deadline_upload(self, v: Verifier, work,
                                completed: list, unfinished: list) -> None:
        self.stats["uploaded"] += v.mark_uploaded(work, completed)
        for task in completed:
            self.task_status.mark_submitted(
                v.vid, task,
                int(((task.result or {}).get("work") or 0)), time.time())
        self._remember_board_batch(v, work, completed)
        promoted = v.finish_work(work)
        if promoted is not None:
            self.log.event("queue_promoted", verifier=v.vid,
                           lease=promoted.lease_id,
                           work_kind=promoted.kind,
                           source=promoted.source, via="deadline")
        self.clear_slot_next(v, work)
        if unfinished:
            self.task_status.mark_returned(v.vid, work, "deadline cutoff")
            self.note_returned(len(unfinished))
        self.on_event("deadline", f"{v.label} board pack cutoff: "
                                  f"uploaded {len(completed)}, "
                                  f"aborted {len(unfinished)}")

    async def _finalize_board_deadline(self, v: Verifier, work,
                                       restart_engine: bool = True) -> None:
        """STOP THIS PACK — THE FINISHED ROWS RIDE THE NEXT SYNC.

        Used both by the safety cutoff (where the engine must be killed to stop
        a fold that has already lost its lease) and by pause (where the last row
        has just finished and there is nothing to kill).

        THERE IS NO UPLOAD HERE ANY MORE.  The classic door posted the finished
        rows to `/api/board/answers` with `done: true`; under protocol 8 every
        finished row is ALREADY kept (`_upload_board` remembers each answer as
        it finishes), so the next sync's `results` carries them, and the rows
        that never finished simply stop being named — which is how protocol 8
        gives a row back.
        """
        if v.work_role(work) is None or work.aborted:
            return
        work.aborted = True
        self._deadline_restart(v, work, restart_engine)
        if not self.paused and not self.maintenance:
            await self._ensure_queue(v)
        completed, unfinished = self._deadline_rows(work)
        self._accept_deadline_upload(v, work, completed, unfinished)


    async def pause_drain(self) -> None:
        """Stop taking work, hand back what has not started, finish what has.

        The point is to stop WITHOUT throwing away progress: a row already on an
        engine is never interrupted, and a pack that is half done sends the rows
        it finished and lets the rest go back with the lease.
        """
        if self.paused:
            return
        self.paused = True
        self.on_event("pause", "已停止申请；正在取消在途请求并归还未开始的任务…")
        # A fetch already in flight can still land a lease after `paused` is
        # set.  Cancel it first, then give back anything that landed before
        # cancellation; otherwise update_idle() can wait forever on a lease
        # no engine is allowed to start.
        pending = [t for t in self.fetch_tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.fetch_tasks.clear()
        for v in self.verifiers:
            v.cancel_fetch("pause")
        # A work with a row on an engine is drained, not dropped; one with
        # nothing running goes straight back.  BOTH roles are treated alike:
        # the ahead work may be on an engine too.
        if self._board_sync_active():
            # MARK THE UNSTARTED ROWS GONE *BEFORE* THE ROUND, not after it.
            # `_sync_held_rows` names everything not `lost`, and the sync loop
            # keeps beating while this awaits the network: released-then-named
            # rows come home as `held:false` and every one of them is reported
            # as "被收走" and counted as an invalid.  Measured: a 34-row pack
            # was handed out by a sync that was already on the wire when the
            # drain started, and the loop named all 34 after the round had
            # released them — one long chain of bogus losses.
            for v in list(self.verifiers):
                for work, _role in list(v.works()):
                    if work is None or work.aborted or work.kind != "board":
                        continue
                    if not self._sync_source(work):
                        continue
                    for t in work.tasks:
                        if t.state == "pending":
                            t.lost = True
            # One final sync hands in results and releases every unstarted row
            # server-side (in-flight rows stay named).  The loop below then
            # only detaches, without redundant release POSTs.
            await self._sync_final_round(hold_inflight=True)
        for v in list(self.verifiers):
            for work, _role in list(v.works()):
                if work.aborted:
                    continue
                if (work.kind == "riven_gain"
                        and self._riven_pending_exists(work.lease_id)):
                    await self._detach_pending_riven(v, work)
                    continue
                if work.kind == "board" and self._sync_source(work):
                    if work.inflight_tasks():
                        # THE ROWS THE ROUND ABOVE JUST RELEASED MUST GO NOW,
                        # locally too: `_sync_held_rows` names everything not
                        # `lost`, so a pack left with its unstarted rows intact
                        # kept asking the server to renew rows the server had
                        # already taken back — one sync replied `held:false`
                        # for 27 rows and the UI reported all 27 as "被收走".
                        # What is still on an engine finishes; the rest is
                        # given up here, once.
                        for t in work.tasks:
                            if t.state == "pending":
                                t.lost = True
                        work.pausing = True
                        self.on_event("pause",
                                      f"{v.label} 正在算的这一行算完再还，"
                                      f"其余已归还")
                    else:
                        await self._detach_sync_paused_work(v, work, "pause")
                    continue
                if work.inflight_tasks():
                    work.pausing = True
                    self.on_event("pause", f"{v.label} 正在算的这一行算完再还")
                else:
                    await self._give_back_work(v, work, "pause")
        self.on_event("pause", "排空中：算完的最后几行上传后即完成暂停")

    async def _detach_sync_paused_work(self, v: Verifier, work,
                                       why: str) -> None:
        """Detach a sync pack already released server-side (no POST)."""
        if v.work_role(work) is None:
            return
        for t in work.tasks:
            if t.state == "pending":
                t.lost = True
        role = v.work_role(work)
        if role == "primary":
            v.give_back(why)
        elif role == "ahead":
            v.give_back_queued(why)
        self.task_status.mark_returned(v.vid, work, why)
        self.clear_slot_next(v, work)
        self.note_returned()
        self.on_event("release", f"{v.label} gave back {work.kind} ({why})")

    async def wait_idle(self, timeout: float = 1900.0) -> bool:
        """Wait until no engine has work and no lease is still held."""
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            if self.update_idle():
                return True
            await asyncio.sleep(0.5)
        return False

    async def change_slot_count(self, target: int,
                                drain_timeout: float = 1900.0) -> bool:
        """PAUSE, DRAIN, REBUILD THE ENGINE POOL ON THE NEW COUNT, RESUME.

        The count only ever moves between rows: stop taking work, let the rows
        already on an engine finish and upload, stop the engines, write the new
        count, start that many, then resume.  Threads are re-planned for the new
        count by `_plan_lanes` on the way back up, so a change to 1 or to 10
        redistributes lanes instead of keeping the old split.
        """
        current = self.slot_count()
        target = config.clamp_slots(target)
        if target == current and self.running:
            return True
        was_paused = bool(self.paused)
        self.maintenance = True
        self.on_event("settings", f"计算进程 {current} -> {target}：先暂停排空")
        try:
            await self.pause_drain()
            if not await self.wait_idle(drain_timeout):
                raise TimeoutError("等待计算进程空闲超时")
            await self.stop()
            self.cfg["slots"] = target
            try:
                self.on_config_changed()
            except Exception:
                pass
            await self.start()
        except Exception as e:
            self.cfg["slots"] = current
            self.maintenance = False
            self.on_event("error", f"计算进程数调整失败：{type(e).__name__}: {e}")
            if not self.running:
                try:
                    await self.start()
                except Exception as e2:
                    self.on_event("error",
                                  f"按原进程数恢复失败：{type(e2).__name__}: {e2}")
            if self.running:
                if was_paused:
                    self.paused = True
                else:
                    await self.resume_from_pause()
            return False
        if was_paused:
            self.paused = True
            self.on_event("settings", f"计算进程 {target} 已就绪；保持用户暂停")
        else:
            await self.resume_from_pause()
            self.on_event("settings", f"计算进程 {target} 已就绪，继续接任务")
        return True

    async def _restart_parked_slots(self) -> None:
        if not self.stopped_slots:
            return
        self.on_event("pause", f"恢复：重启 {len(self.stopped_slots)} 个计算进程")
        for slot in sorted(self.stopped_slots):
            try:
                if self.engines[slot].proc is None:
                    await self._cpu(self.engines[slot].start, self.cpu_limit)
            except Exception as e:
                self.on_event("error", f"恢复引擎 {slot} 失败: {e}")

    def _clear_pausing_flags(self) -> None:
        for v in self.verifiers:
            for work, _ in v.works():
                work.pausing = False

    async def resume_from_pause(self) -> None:
        """Back to three engines, each on its own share."""
        # KEEP THE GATE CLOSED WHILE THE PARKED ENGINES COME BACK.  The slot
        # supervisors run continuously; if `paused` were cleared first they
        # could take work before a restarted engine is listening.  Once every
        # engine is up, the next loop round is free to fetch normally.
        await self._restart_parked_slots()
        self.paused = False
        self.stopped_slots.clear()
        self.drained_lanes = 0
        self._clear_pausing_flags()
        self.on_event("pause", f"已恢复：{self.slot_count()} 个计算进程各用各的线程")

    async def _stop_engine(self, slot: int) -> None:
        """Stop an engine OFF the shared engine pool.

        `ENGINE_POOL` also carries `/api/optimize` calls, which are allowed to
        run for an HOUR (`api.py` `timeout=3600`).  A stop queued behind one of
        those never runs — and during shutdown that is indistinguishable from a
        hang: the console stops repainting and the window never closes.
        `asyncio.to_thread` uses the default executor, which carries no such
        work.
        """
        try:
            await asyncio.to_thread(self.engines[slot].stop)
        except Exception as e:
            self.on_event("error", f"停止引擎 {slot} 失败: {type(e).__name__}: {e}")

    async def _idle_release(self, slot: int) -> None:
        """2.4 IDLE RECYCLE: give an idle engine's memory back.

        Batches arrive minutes apart (`backlog.py`: median wait 463 s), and a
        client engine that is merely idle still holds its loaded tables (42-61
        MB measured).  ONE engine (`idle_keep`, default 1) ALWAYS STAYS UP, and
        nothing here touches lanes: the lane count is a per-request argument
        (`lanes_for_slot`), not a process resource, so giving an engine's
        memory back does not re-share anything.

        Unlike pause, this must never strand work: `_idle_wake` is written to
        run BEFORE the row that woke the slot touches the engine.
        """
        if (slot in self.idle_stopped or slot in self.stopped_slots
                or slot in self.slot_restarting):
            return
        try:
            if self.slots[slot].uploading:
                return
        except Exception:
            pass
        if self.slot_task.get(slot) is not None:
            return
        keep = max(1, self._cfg_int("idle_keep", 1))
        if len(self.slots) - len(self.idle_stopped) <= keep:
            return                      # 保底: never reclaim the last engines
        self.idle_stopped.add(slot)
        await self._stop_engine(slot)
        self.log.event("idle_recycle", slot=slot, keep=keep)

    async def _idle_wake(self, slot: int) -> None:
        """2.4: work arrived for a slot whose engine we reclaimed.

        Called with the task ALREADY reserved, and awaited BEFORE the compute
        call — so the row is never handed to a stopped engine.  Idempotent: a
        slot that was not reclaimed (or whose engine came back on its own) is
        left alone.
        """
        if slot not in self.idle_stopped:
            return
        self.idle_stopped.discard(slot)
        try:
            if self.engines[slot].proc is None:
                await self._cpu(self.engines[slot].start, self.cpu_limit)
            self.log.event("idle_recycle_wake", slot=slot)
        except Exception as e:
            self.on_event("error",
                          f"唤醒引擎 {slot + 1} 失败: {type(e).__name__}: {e}")

    async def _park_slot(self, slot: int) -> None:
        """Pause: an engine with nothing left stops and gives its lanes away."""
        if slot in self.stopped_slots or slot in self.slot_restarting:
            return
        if self.slot_task.get(slot) is not None:
            return
        try:
            if self.slots[slot].uploading:
                return
        except Exception:
            pass
        self.stopped_slots.add(slot)
        self.drained_lanes += max(1, int(self.slot_lanes[slot]))
        await self._stop_engine(slot)
        self.on_event("pause", f"计算进程 {slot + 1} 已释放，线程并入还在算的进程")

    # ------------------------------------------------------- riven concentration
    def riven_total(self) -> int:
        """Every riven row we hold, IN FLIGHT INCLUDED — a display figure now.

        The fetch gate uses `pool_riven()` (pending only), because a riven that
        is already being computed should not stop us asking for another.
        """
        total = 0
        for v in self.verifiers:
            if v.lease is not None and v.lease.kind == "riven_gain" and not v.lease.all_done():
                total += 1
            if v.queued is not None and v.queued.kind == "riven_gain":
                total += 1
        return total

    def riven_progress(self, v: Verifier) -> float | None:
        """Real optimizer progress for any riven this verifier is running."""
        st = next((x for x in self.slots
                   if x.verifier == v.vid and x.kind == "riven_gain"), None)
        if st is None:
            return None
        pct = (st.progress or {}).get("pct")
        try:
            return float(pct) if pct is not None else None
        except Exception:
            return None

    def lanes_for_slot(self, slot: int) -> int:
        """This slot's own share, plus an equal part of anything merged in.

        Running normally every slot uses its own lanes and nothing is shared.
        While DRAINING a pause, the slots that ran out of work are shut down and
        their lanes are handed to the ones still computing, so the last rows go
        as fast as the surviving engines can take them.

        RESTORED 2026-10-16 07:15: the protocol-7 deletion took this method with
        it.  The slot loop calls it every cycle (`_lanes_for_row`), so every slot
        raised `AttributeError` and went through `slot_recover_begin/done` in a
        loop — which re-pinned the engines over and over, starting from the
        lowest logical processors, and that is what "一直从小分配核心" was.
        """
        try:
            base = max(1, int(self.slot_lanes[slot]))
        except Exception:
            base = max(1, int(self.cfg.get("lanes_per_slot") or 1))
        if self.drained_lanes <= 0 or slot in self.stopped_slots:
            return base
        live = [i for i in range(len(self.slot_lanes)) if i not in self.stopped_slots]
        if not live or slot not in live:
            return base
        share, rem = divmod(self.drained_lanes, len(live))
        return base + share + (1 if live.index(slot) < rem else 0)

    def lanes_for_task(self, slot: int, task: WorkTask) -> int:
        """Threads this slot may use — its own share, plus any merged in when
        the other slots finished and were shut down during a pause."""
        return self.lanes_for_slot(slot)

    def _riven_progress(self, slot: int, st: dict) -> None:
        planned = st.get("sims_planned") or 0
        done = st.get("sims_done") or 0
        # `quick` uses `enumerated`/`sims_done`; the funnel's round fields stay
        # at zero there and must not be shown as if the search had stalled.
        self.slots[slot].progress.update({
            "phase": st.get("phase"),
            "round": st.get("round"), "rounds": st.get("rounds"),
            "jobs": st.get("round_jobs"), "runs": st.get("round_runs"),
            "enumerated": st.get("enumerated"),
            "sims_done": done, "sims_planned": planned,
            "pct": (done / planned) if planned else None,
        })

    def _log_fight_start(self, slot: int, v: Verifier, task: WorkTask, work) -> str:
        tag = f"{work.lease_id}:{task.question or task.index}"
        self.log.task(work.lease_id, compute_start={
            "slot": slot, "verifier": v.vid, "kind": task.kind,
            "source": work.source, "lease": work.lease_id,
            "question": task.question, "task_index": task.index,
            "task_total": task.total,
            "engine_port": self.engines[slot].port,
            "lanes": self.lanes_for_task(slot, task),
        }, task_input=task.task)
        return tag

    async def _fight_board(self, slot: int, v: Verifier, task: WorkTask,
                           work, tag: str) -> dict:
        lanes = self.lanes_for_task(slot, task)

        def call():
            return self._cpu(
                self.engines[slot].board_row, task.task["record"],
                task.task["ruler"], task.task["mode"], lanes, log_tag=tag)

        if lanes <= 1:
            res = await self._cpu_on_primary(slot, call)
        else:
            res = await call()
        self.stats["board"] += 1
        if getattr(self.log, "full", False):
            self.log.task(work.lease_id, compute_result=res, question=task.question)
        else:
            self.log.task(work.lease_id, question=task.question,
                          compute_result={"score": res.get("score"),
                                          "metric": res.get("metric"),
                                          "work": res.get("work"),
                                          "counts": res.get("counts") or {}})
        return {"kind": "board", "score": res["score"], "metric": res["metric"],
                "work": res["work"], "counts": res.get("counts") or {},
                "payload": None}

    async def _fight_board_batch(self, slot: int, v: Verifier, tasks: list,
                                 work, tags: list) -> list:
        """N board tasks in ONE engine call — 2.2c's engine-side entry point.

        WHY: a row's serial `score` measured 6-20 ms on rows of 0.47-2.44 s
        (1.2-4.3%), and the lanes idle through it.  The engine's
        `/api/board/rows` pipelines that: each row's score overlaps the NEXT
        row's fold, folds stay strictly serial (the lane pool is never asked for
        its threads twice).  30 rows in batches of 8 measured **3.9% faster**
        than one call each, bit-identical (0/30).

        RETURNS ONE ENTRY PER TASK, IN ORDER.  An entry is either the same
        `{"kind": "board", ...}` dict `_fight_board` returns, or the exception
        the engine reported for THAT row (`RowRefused` for a row the board's own
        validation refused, `RuntimeError` otherwise) — exactly the per-row
        policy `board_rows_batch` documents.  THE CALLER STILL OWNS EVERY PER-ROW
        DECISION (hand-in, anchor, ledger): the win here is the engine-side
        pipeline, not client-side concurrency, so no per-row semantics move.
        """
        lanes = self.lanes_for_task(slot, tasks[0])
        rows = [(t.task["record"], t.task["ruler"], t.task["mode"]) for t in tasks]

        def call():
            return self._cpu(self.engines[slot].board_rows_batch, rows, lanes)

        if lanes <= 1:
            res = await self._cpu_on_primary(slot, call)
        else:
            res = await call()
        if not isinstance(res, list) or len(res) != len(tasks):
            raise RuntimeError("board batch came back as %s" % type(res).__name__)
        out = []
        for task, one, tag in zip(tasks, res, tags):
            if isinstance(one, Exception):
                out.append(one)
                continue
            self.stats["board"] += 1
            if getattr(self.log, "full", False):
                self.log.task(work.lease_id, compute_result=one,
                              question=task.question)
            else:
                self.log.task(work.lease_id, question=task.question,
                              compute_result={"score": one.get("score"),
                                              "metric": one.get("metric"),
                                              "work": one.get("work"),
                                              "counts": one.get("counts") or {}})
            out.append({"kind": "board", "score": one["score"],
                        "metric": one["metric"], "work": one["work"],
                        "counts": one.get("counts") or {}, "payload": None})
        return out

    def _log_riven_result(self, work, r: dict, best) -> None:
        if getattr(self.log, "full", False):
            self.log.task(work.lease_id, compute_result=r, compute_best=best)
            return
        self.log.task(work.lease_id, compute_result={
            "work": r.get("work"),
            "search_work": r.get("search_work"),
            "final_work": r.get("final_work"),
            "builds": r.get("builds"),
            "fights": r.get("fights"),
            "duration": r.get("duration"),
            "results": len(r.get("results") or []),
        })

    @staticmethod
    def _riven_work(r: dict) -> int:
        # THE NATIVE OPTIMIZER REPORTS THE FINAL ROUND AS `final_work`.
        work = int(r.get("work") or 0)
        return work or int(r.get("final_work") or 0)

    def _riven_result(self, r: dict, best: dict, task: WorkTask) -> dict:
        return {"kind": "riven_gain",
                "score": float(best.get("kill_progress") or best.get("kills") or 0),
                "work": self._riven_work(r),
                "builds": int(r.get("builds") or 0),
                "fights": int(r.get("fights") or 0),
                "duration": float(r.get("duration") or 0.0),
                "enumerated": int((best or {}).get("enumerated")
                                   or r.get("builds") or r.get("enumerated") or 0),
                "payload": board_payload_from_result(
                    best, task.task.get("context") or {})}

    async def _fight_riven(self, slot: int, v: Verifier, task: WorkTask,
                           work, tag: str) -> dict:
        riven_step = functools.partial(self._riven_progress, slot)
        lanes = self.lanes_for_task(slot, task)

        def call():
            return self._cpu(
                self.engines[slot].riven_gain, task.task["request"],
                lanes, riven_step,
                log_tag=tag)

        if lanes <= 1:
            r = await self._cpu_on_primary(slot, call)
        else:
            r = await call()
        best = next((x for x in (r.get("results") or []) if x and x.get("mods")), None)
        self._log_riven_result(work, r, best)
        if best is None:
            raise RuntimeError("the search returned no build")
        self.stats["riven"] += 1
        return self._riven_result(r, best, task)

    async def _fight(self, slot: int, v: Verifier, work,
                     task: WorkTask) -> dict:
        """COMPUTE ONE WorkTask. No upload and no verifier transition here."""
        tag = self._log_fight_start(slot, v, task, work)
        if not self.log.full:
            if task.kind == "board":
                return await self._fight_board(slot, v, task, work, tag)
            return await self._fight_riven(slot, v, task, work, tag)
        proc = getattr(self.engines[slot], "proc", None)
        cpu0 = cores.process_cpu_seconds(proc.pid) if proc is not None else None
        t0 = time.time()
        try:
            if task.kind == "board":
                return await self._fight_board(slot, v, task, work, tag)
            return await self._fight_riven(slot, v, task, work, tag)
        finally:
            # 完整 ONLY: how much CPU this one row's engine call actually burned
            # against its lane budget.  It is what separates "the row is heavy"
            # from "the row ran on fewer lanes than it was given" — a fleet that
            # looks 99% busy at the process level can still be short a lane or
            # two, and only the ratio per row shows it.
            self._log_engine_cpu(slot, v, task, cpu0, t0)

    def _log_engine_cpu(self, slot: int, v: Verifier, task: WorkTask,
                        cpu0, t0: float) -> None:
        """完整 ONLY: one engine call's CPU seconds vs wall vs lane budget.

        `cores_used` is CPU-seconds over wall seconds: it is how many cores the
        engine process kept busy on average for this row.  Compare it with
        `lanes` — a row asked for N lanes that comes back with far fewer cores
        actually used spent the difference NOT computing (a serial order/fold
        tail, a thread waiting on a lock, a run that never got scheduled).
        """
        e = self.engines[slot]
        pid = e.proc.pid if e.proc is not None else None
        cpu1 = cores.process_cpu_seconds(pid) if pid else None
        wall = max(1e-6, time.time() - t0)
        lanes = self.lanes_for_task(slot, task)
        data = {
            "slot": slot, "verifier": v.vid,
            "work_kind": task.kind, "engine_port": e.port,
            "lanes": lanes, "wall_s": round(wall, 3),
            "tag": f"{task.question or task.index}",
        }
        if cpu0 is not None and cpu1 is not None:
            used = cpu1 - cpu0
            data["cpu_s"] = round(used, 3)
            data["cores_used"] = round(used / wall, 3)
            data["lane_eff"] = round((used / wall) / max(1, lanes), 3)
        self.log.event("engine_cpu", **data)

    def _log_slot_idle(self, slot: int, seconds: float) -> None:
        """完整 ONLY: one slot's idle spell, with the reason it had nothing.

        `reason` is the diagnosis a reader would otherwise have to infer:
        `paused`/`maintenance` is deliberate, `pool-empty` is the fleet having
        no unstarted row anywhere (a supply problem, not a scheduling one),
        and `all-taken` is every remaining row already on another engine (the
        slot lost the race — the benign case).
        """
        if seconds < 0.25:
            return                    # the ordinary hand-off tick, not a stall
        if self.paused:
            reason = "paused"
        elif self.maintenance:
            reason = "maintenance"
        else:
            try:
                pending = sum(1 for x in self.verifiers
                              for w, _r in x.works() if w is not None
                              for t in w.pending_tasks())
            except Exception:
                pending = -1
            reason = "pool-empty" if pending <= 0 else "all-taken"
        self.log.event("slot_idle", slot=slot,
                       port=self.engines[slot].port,
                       seconds=round(seconds, 3), reason=reason)

    def _slot_result(self, slot: int, v: Verifier, task: WorkTask, ok) -> None:
        """The previous row of this engine is settled — say so, and when."""
        st = self.slots[slot]
        st.uploading = None
        st.uploading_lease = None
        st.last_verifier, st.last_label = v.vid, task.label()
        st.last_ok, st.last_finished_at = ok, time.time()
        if task.result:
            counts = task.result.get("counts") or {}
            if counts:
                st.last_counts = dict(counts)
            elif task.kind == "riven_gain":
                st.last_counts = {"builds": task.result.get("builds", 0),
                                  "fights": task.result.get("fights", 0)}
        st.done += 1

    def note_board_time(self, ms: int) -> None:
        """Update the pace_ms the server uses to size the next pack."""
        if ms <= 0:
            return
        if self.pace_ms <= 0:
            self.pace_ms = ms
        else:
            self.pace_ms = max(100, int(self.pace_ms * 0.8 + ms * 0.2))

    async def _flush_board_answers(self, v: Verifier, work,
                                    tries: int | None = None) -> bool:
        """Serialize board-answer flushes for one lease."""
        lock = self.upload_locks.setdefault(work.lease_id, asyncio.Lock())
        async with lock:
            return await self._flush_board_answers_locked(v, work, tries)

    @staticmethod
    def _board_unsent(work) -> list:
        return [t for t in work.tasks
                if t.result is not None and not t.uploaded and not t.lost]

    @staticmethod
    def _board_order_of(task) -> tuple[str, str, str]:
        raw = task.task if isinstance(getattr(task, "task", None), dict) else {}
        return (str(raw.get("identity") or ""),
                str(raw.get("ruler") or ""),
                str(raw.get("mode") or (raw.get("record") or {}).get("mode") or ""))

    def _remember_board_batch(self, v: Verifier, work, tasks: list) -> None:
        """Keep sent board answers in memory until `/mine` confirms them."""
        lanes = int(self.cfg.get("work_lanes") or 16)
        now = time.time()
        for t in tasks:
            if t.result is None or not t.question:
                continue
            identity, ruler, mode = self._board_order_of(t)
            self._board_kept[(work.lease_id, t.question)] = {
                "vid": v.vid, "engine": self.engine_id,
                "answer": answer_payload(t, t.result, t.compute_ms or 0, lanes),
                "identity": identity, "ruler": ruler, "mode": mode,
                "kept_at": now, "resent": 0,
            }
        self._sweep_board_kept(now)

    def _sweep_board_kept(self, now: float | None = None) -> int:
        """Drop over-TTL entries and bound memory; returns dropped count."""
        now = time.time() if now is None else float(now)
        dropped = 0
        for key, entry in list(self._board_kept.items()):
            if now - float(entry.get("kept_at") or 0.0) >= BOARD_KEPT_TTL_S:
                # pop, not del: another coroutine (a row loss, a reconcile)
                # may have taken this key between the snapshot and here, and
                # a KeyError here killed the whole status poll.
                self._board_kept.pop(key, None)
                dropped += 1
        while len(self._board_kept) > BOARD_KEPT_MAX:
            oldest = min(self._board_kept.items(),
                         key=lambda kv: float(kv[1].get("kept_at") or 0.0))[0]
            self._board_kept.pop(oldest, None)
            dropped += 1
        if dropped:
            self.log.event("board_kept_dropped", dropped=dropped,
                           reason="ttl/cap", kept=len(self._board_kept))
        return dropped

    def _drop_board_kept(self, lease: str, question: str | None = None,
                         reason: str = "") -> int:
        """Forget kept answers that can never land (row lost)."""
        keys = [k for k in self._board_kept
                if k[0] == lease and (question is None or k[1] == question)]
        for k in keys:
            self._board_kept.pop(k, None)
        if keys:
            self.log.event("board_kept_dropped", lease=lease,
                           question=question, reason=reason,
                           dropped=len(keys), kept=len(self._board_kept))
        return len(keys)

    def _finish_board_pack(self, v: Verifier, work) -> None:
        promoted = v.finish_work(work)
        if promoted is not None:
            self.log.event("queue_promoted", verifier=v.vid,
                           lease=promoted.lease_id,
                           work_kind=promoted.kind,
                           source=promoted.source, via="pack_done")
        runs = sum(int(((t.result or {}).get("counts") or {}).get("runs") or 0)
                   for t in work.tasks)
        shots = sum(int(((t.result or {}).get("counts") or {}).get("shots") or 0)
                    for t in work.tasks)
        self.on_event("upload", f"{v.label} -> board pack "
                                f"{len(work.tasks)} row(s) ok "
                                f"fights={runs} shots={shots}")
        self.upload_locks.pop(work.lease_id, None)

    async def _flush_board_answers_locked(self, v: Verifier, work,
                                             tries: int | None = None) -> bool:
        """NOTHING GOES OUT HERE: THIS CLIENT SPEAKS PROTOCOL 8 ONLY.

        A board answer rides the next sync's `results` — `_upload_board` keeps
        it and returns — and the classic `/api/board/answers` door is never
        posted to.  The 2.26 h reference session shows exactly that split: 1689
        `/api/board/sync` requests against ZERO `/api/board/answers`.

        The method stays, and stays honest, because four callers still ask it
        whether the pack can move on (the slot loop's refusal tail,
        `_retry_stalled_board`, `_retry_held_submissions`,
        `_release_held_after_queue`).  There is nothing to flush, so it has
        nothing to do and never fails: the next sync carries it.
        """
        return True

    async def _upload_board(self, slot: int, v: Verifier, work, task: WorkTask,
                            res: dict, ms: int) -> None:
        v.record_task_answer(work, task, res, ms)
        self.note_board_time(ms)
        self._slot_result(slot, v, task, True)
        if work.kind == "board" and self._sync_source(work):
            # Protocol 8: no `/answers` POST — the answer rides the next sync's
            # `results` (kept until `taken`/`dropped`).  The ledger row keeps
            # the UI honest until then.
            self.task_status.mark_submitted(
                v.vid, task, int(((res or {}).get("work") or 0)), time.time())
            self._remember_board_batch(v, work, [task])
            if work.pausing and not work.inflight_tasks():
                # THE PAUSE COMPLETES HERE.  The last row on an engine is in
                # and kept (still named, so its lease survives until `taken`);
                # the unstarted rows went back at pause time.  One last sync
                # hands the answers in, then the pack is detached — without
                # this the paused pack just sat on the verifier and the only
                # thing that ever closed it was a watchdog reset.
                await self._sync_final_round(hold_inflight=True)
                await self._detach_sync_paused_work(v, work, "pause done")
            return
        # Pausing keeps its special path: it sends what is finished and
        # gives back the rest instead of waiting for another row.
        if work.pausing and not work.inflight_tasks():
            await self._finalize_board_deadline(v, work, restart_engine=False)
            return
        ok = await self._flush_board_answers(v, work)
        if ok:
            return
        if v.work_all_done(work):
            # STALLED, NOT LOST: the tail cannot reach the server, but the
            # rows are finished and the answers sit in memory (`task.result`
            # plus the `_board_kept` copies from the batches that did land).
            # Giving the pack back here would throw finished work away, so the
            # verifier keeps holding it: `_retry_stalled_board` re-flushes
            # every 30 s until it lands, renewals keep the lease meanwhile, and
            # `hard_deadline` (hours out) is the only thing that lets go.
            # The ledger stays SUBMITTED so `/mine` keeps polling it.
            self.log.event("board_pack_stalled", verifier=v.vid,
                           lease=work.lease_id, rows=len(work.tasks))
            self.on_event("error", f"{v.label} 尾巴断网，答案留内存等重连补发，"
                                   f"暂不退回（{len(work.tasks)} 行）")
            return
        self.on_event("error", f"{v.label} board batch upload "
                               f"refused; keeping rows for retry")

    def _riven_upload_payload(self, v: Verifier, work, res: dict,
                              ms: int) -> dict:
        # A riven gain is one task and keeps its own result endpoint.  The
        # server credits `work`; `compute_ms`, `lanes` and `counts` are kept
        # beside it for statistics, exactly as the official page sends them.
        return {"build": res["payload"], "lease": work.lease_id,
                "verifier": v.vid, "engine": self.engine_id,
                "score": res["score"], "work": res["work"],
                "compute_ms": ms,
                "lanes": int(self.cfg.get("work_lanes") or 16),
                "counts": {"builds": int(res.get("builds") or 0),
                           "fights": int(res.get("fights") or 0),
                           "steps": int(res.get("enumerated") or 0)}}

    def _keep_pending_riven(self, v: Verifier, work, payload: dict) -> None:
        """Keep the finished answer in memory BEFORE it is sent."""
        now = time.time()
        raw = getattr(work, "raw", None) or {}
        self._riven_kept[work.lease_id] = {
            "code": work.code, "lease": work.lease_id, "verifier": v.vid,
            "weapon": str(raw.get("weapon") or ""),
            # `appraise/mine` needs both a code and a timestamp; the
            # code matches, the timestamp is what its filter reads.
            "at": now,
            "written_at": now,
            "expires_at": now + RIVEN_PENDING_KEEP_S,
            "payload": payload}

    def _drop_pending_riven(self, lease: str) -> None:
        self._riven_kept.pop(lease, None)

    def _riven_pending_exists(self, lease: str) -> bool:
        """True while a finished, undelivered answer for this lease is kept."""
        return lease in self._riven_kept

    def _read_pending_riven(self, lease: str) -> dict | None:
        return self._riven_kept.get(lease)

    def _touch_pending_riven(self, lease: str, keep_s: float) -> None:
        """Extend a kept answer's window while its lease is still renewable."""
        entry = self._riven_kept.get(lease)
        if entry is not None:
            entry["expires_at"] = time.time() + float(keep_s)

    @staticmethod
    def _riven_answer_retryable(out) -> bool:
        """A transient failure keeps the answer; a refusal does not.

        `Remote.post` reports a transport fault as `transport_error` and a
        status code as `http_error`.  Only a definite refusal — a bad answer, or
        an appraisal the server no longer keeps — may throw a result away.
        """
        if not isinstance(out, dict):
            return True
        if out.get("transport_error"):
            return True
        code = out.get("http_error")
        if not isinstance(code, int):
            return False
        return code >= 500 or code in (403, 429)

    async def _riven_mine_state(self, entry) -> str | None:
        """The server's own state for a kept answer: confirmed/waiting/gone."""
        vid = entry.get("verifier")
        code = str(entry.get("code") or "")
        if not vid or not code:
            return None
        at = float(entry.get("at") or entry.get("written_at") or 0.0)
        try:
            out = await self._io(self.remote.appraise_mine, vid,
                                 [{"code": code,
                                   "weapon": str(entry.get("weapon") or ""),
                                   "at": at}])
        except Exception as e:
            self.log.event("riven_mine_error", code=code,
                           error=f"{type(e).__name__}: {e}")
            return None
        if not isinstance(out, dict) or not out.get("ok"):
            return None
        for item in out.get("tasks") or []:
            if isinstance(item, dict) and str(item.get("code") or "") == code:
                return str(item.get("state") or "") or "absent"
        # The server answered, and it holds no answer of ours for this code:
        # that is evidence, not an unknown.  `None` is kept for a failed call.
        return "absent"

    async def _resolve_closed_lease(self, v: Verifier, work) -> None:
        """The server says this lease is closed: record it or let it go.

        `held:false` is ambiguous — our own answer clears the lease too — so any
        copy still on the wire is waited out and `appraise/mine` decides.  A
        recorded answer is closed as delivered; a lease that is gone with no
        record can never take the answer (`answer()` matches a live lease), so
        holding it would only park the verifier.
        """
        lease = work.lease_id
        await self._wait_riven_sends(lease)
        entry = self._read_pending_riven(lease)
        if entry is None:
            return                    # the copy landed; that path closed the work
        state = await self._riven_mine_state(entry)
        if state in ("confirmed", "waiting"):
            self._drop_pending_riven(lease)
            self.log.event("riven_pending_recorded", lease=lease,
                           code=entry.get("code"), state=state, via="renew")
            await self._finish_pending_riven(vid=entry.get("verifier"), lease=lease)
            return
        if state is None:
            # The check itself failed: keep the answer for the next beat.
            self.log.event("riven_pending_unconfirmed", lease=lease,
                           code=entry.get("code"), state=None, via="renew")
            return
        self._drop_pending_riven(lease)
        self.log.event("riven_pending_dropped", lease=lease,
                       code=entry.get("code"), state=state, via="renew closed")
        await self._release_pending_riven(vid=entry.get("verifier"), lease=lease)

    async def _expire_pending_riven(self, entry) -> bool:
        """The keep window ran out: only drop what the server does not hold."""
        lease = str(entry.get("lease") or "")
        state = await self._riven_mine_state(entry)
        if state in ("confirmed", "waiting"):
            self._drop_pending_riven(lease)
            self.log.event("riven_pending_recorded", lease=lease,
                           code=entry.get("code"), state=state, via="expiry")
            await self._finish_pending_riven(vid=entry.get("verifier"), lease=lease)
            return True
        self._drop_pending_riven(lease)
        self.log.event("riven_pending_dropped", lease=lease,
                       code=entry.get("code"), state=state,
                       waited_s=int(time.time() - float(entry.get("written_at") or 0.0)))
        await self._release_pending_riven(vid=entry.get("verifier"), lease=lease)
        return False

    def _pending_riven_entries(self) -> list:
        """Every answer still waiting, oldest first."""
        return sorted(self._riven_kept.items(),
                      key=lambda kv: float(kv[1].get("written_at") or 0.0))

    def _riven_pending_hold_max(self) -> float:
        """Seconds a finished answer may hold a verifier (0 disables)."""
        raw = (getattr(self, "cfg", None) or {}).get("riven_pending_hold_seconds")
        if raw is None:
            return RIVEN_PENDING_HOLD_S
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return RIVEN_PENDING_HOLD_S

    async def _retry_pending_riven(self) -> None:
        """RE-SEND WHAT WAS FINISHED, until the server takes it.

        A riven's answer is minutes of search and there is no second chance at
        it, so a send that fails is kept and tried again rather than thrown
        away: the lease is renewed while the kept answer waits and the server accepts
        the answer under it.  `first:false` is NOT a loss — a duplicate, or a
        copy arriving after our own earlier answer cleared the lease, reads the
        same — so `appraise/mine` decides: confirmed/waiting closes the work,
        gone releases it, anything else keeps the answer.  Only the keep window
        running out, or a definite refusal, gives a result up.
        """
        now = time.time()
        if now < self._pending_riven_at:
            return
        self._pending_riven_at = now + RIVEN_PENDING_EVERY_S
        hold = self._riven_pending_hold_max()
        for lease, entry in self._pending_riven_entries()[:RIVEN_PENDING_PER_PASS]:
            lease = str(entry.get("lease") or lease)
            waited = now - float(entry.get("written_at") or now)
            if hold and waited > hold:
                # A LOST ANSWER IS BETTER THAN A PARKED VERIFIER.  The kept
                # answer has been re-sent again and again; the confirmation
                # path itself is the thing that is broken now.
                self._drop_pending_riven(lease)
                self.log.event("riven_pending_hold_expired", lease=lease,
                               code=entry.get("code"), waited_s=int(waited))
                await self._release_pending_riven(vid=entry.get("verifier"),
                                                  lease=lease)
                continue
            await self._send_pending_riven(lease, entry)

    async def _send_pending_riven(self, lease: str, entry: dict) -> bool:
        """ONE attempt at one kept answer.  True when the server has it.

        `first:false` DOES NOT MEAN LOST.  It is `taken` from
        `facts().answer` (worker/appraise.js `volunteerAnswer`), and a question's
        lease is cleared by a successful answer — so our own retry, or a copy
        already on the wire, reads false although the result WAS recorded.  Only
        `appraise/mine` can tell that apart from a genuine loss, so it decides.
        """
        lease = str(entry.get("lease") or "")
        payload = entry.get("payload")
        if not lease or not isinstance(payload, dict):
            self._drop_pending_riven(lease)
            return False
        if lease in self.riven_sending:
            return False                       # a copy is already on the wire
        now = time.time()
        if now >= float(entry.get("expires_at") or 0):
            return await self._expire_pending_riven(entry)
        code = str(entry.get("code") or "")
        # ONE LEASE, ONE ATTEMPT AT A TIME — held across the `mine` check too,
        # so a pause-time flush cannot start a second copy mid-resolution.
        self.riven_sending.add(lease)
        try:
            out = await self._send_riven_once(code, payload, lease)
            if out is None:
                return False
            if not isinstance(out, dict) or not out.get("ok"):
                return await self._after_riven_refusal(entry, out)
            if out.get("first") is not False:
                self._drop_pending_riven(lease)
                self.log.event("riven_pending_sent", lease=lease, code=code,
                               waited_s=int(now - float(entry.get("written_at") or now)))
                await self._finish_pending_riven(vid=entry.get("verifier"), lease=lease)
                return True
            return await self._after_riven_duplicate(entry, out)
        finally:
            self.riven_sending.discard(lease)

    async def _send_riven_once(self, code: str, payload: dict, lease: str):
        """One POST; a transport fault keeps the answer, it does not lose it."""
        try:
            return await self._io(self.remote.appraise_result, code, payload)
        except Exception as e:
            self.log.event("riven_pending_retry_error", lease=lease,
                           error=f"{type(e).__name__}: {e}")
            return None

    async def _after_riven_refusal(self, entry: dict, out) -> bool:
        """A transient failure keeps the answer; a refusal releases it."""
        lease = str(entry.get("lease") or "")
        if self._riven_answer_retryable(out):
            self.log.event("riven_pending_retry_later", lease=lease,
                           code=entry.get("code"), response=out)
            return False
        self._drop_pending_riven(lease)
        self.log.event("riven_pending_refused", lease=lease,
                       code=entry.get("code"), response=out)
        await self._release_pending_riven(vid=entry.get("verifier"), lease=lease)
        return False

    async def _after_riven_duplicate(self, entry: dict, out) -> bool:
        """`first:false`: ask the server which copy of the answer it kept."""
        lease = str(entry.get("lease") or "")
        state = await self._riven_mine_state(entry)
        if state in ("confirmed", "waiting"):
            self._drop_pending_riven(lease)
            self.log.event("riven_pending_recorded", lease=lease,
                           code=entry.get("code"), state=state)
            await self._finish_pending_riven(vid=entry.get("verifier"), lease=lease)
            return True
        if state in ("gone", "absent"):
            # THE LEASE IS GONE AND NOTHING WAS RECORDED UNDER IT.  `answer()`
            # only matches a live lease, so re-sending can never be taken; let
            # the work go rather than park a verifier on it.
            self._drop_pending_riven(lease)
            self.log.event("riven_pending_dropped", lease=lease,
                           code=entry.get("code"), state=state)
            await self._release_pending_riven(vid=entry.get("verifier"), lease=lease)
            return False
        self.log.event("riven_pending_unconfirmed", lease=lease,
                       code=entry.get("code"), state=state, response=out)
        return False

    async def _flush_pending_riven(self, lease: str | None = None) -> int:
        """DELIVER WHAT IS FINISHED, NOW — before a pause or a quit lets it go.

        One attempt each, no waiting: an answer that is finished but unsent is
        not work in progress to hand back, and giving the lease away first is
        what would make it untakeable (`first: false`).
        """
        sent = 0
        for _lease, entry in self._pending_riven_entries():
            if lease is not None and str(entry.get("lease")) != lease:
                continue
            if await self._send_pending_riven(str(entry.get("lease") or _lease),
                                              entry):
                sent += 1
        if sent:
            self.on_event("upload", f"已算完的紫卡答案补发成功 {sent} 条")
        return sent

    async def _wait_riven_sends(self, lease: str | None = None,
                                timeout: float = 10.0) -> None:
        """Bounded wait for a `/result` POST already on the wire.

        Releasing a lease under an in-flight copy is what makes the server
        answer `first:false`, so give-back and shutdown wait for it to land.
        """
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            busy = [x for x in self.riven_sending
                    if lease is None or x == lease]
            if not busy:
                return
            await asyncio.sleep(0.2)

    async def _detach_pending_riven(self, v: Verifier, work) -> None:
        """A finished, undelivered answer keeps its lease while we are paused.

        Pausing hands work back, but this work IS finished — what is missing is
        the send.  The lease is what makes the retry land, so it stays; the
        verifier is freed locally all the same, and the retry loop keeps trying
        until it arrives or the lease lapses.
        """
        await self._flush_pending_riven(work.lease_id)
        if not self._riven_pending_exists(work.lease_id):
            return                        # it landed; the work is already closed
        task = work.tasks[0] if work.tasks else None
        if task is not None and getattr(task, "result", None):
            # DONE, NOT UPLOADED: ...
            work.record_answer(task, task.result or {}, task.compute_ms or 0)
        elif task is not None:
            # No computed answer to retry: drop the kept entry so the retry
            # loop does not keep poking a finished-but-empty work.
            self._drop_pending_riven(work.lease_id)
        # THE WORK STAYS ON THE VERIFIER.  Dropping it here (the old
        # `finish_work`) also dropped its heartbeat, and a lease nobody renews
        # lapses before the re-send can land.  The retry loop closes it once the
        # server has the answer.
        v.sync_state("pending riven kept")
        self.on_event("pause", f"{v.label} 这条紫卡已算完但没发出去："
                               f"答案留内存并继续续约补发（不浪费）")

    async def _pending_riven_work(self, vid: str | None, lease: str):
        """The work this answer belongs to, if this client still holds it."""
        for v in self.verifiers:
            if vid and v.vid != vid:
                continue
            for work, _role in v.works():
                if work is not None and work.lease_id == lease:
                    return v, work
        return None, None

    async def _finish_pending_riven(self, vid: str | None, lease: str) -> None:
        """The answer landed late: close the work it belonged to."""
        v, work = await self._pending_riven_work(vid, lease)
        if work is None:
            return
        task = work.tasks[0] if work.tasks else None
        if task is not None:
            # `mark_uploaded` is what sets `uploaded` and counts it once: setting
            # the flag first would make a late-delivered answer uncounted.
            v.record_task_answer(work, task, task.result or {}, task.compute_ms or 0)
            self.stats["uploaded"] += v.mark_uploaded(work, [task])
            self.task_status.mark_submitted(
                v.vid, task, int((task.result or {}).get("work") or 0), time.time())
        promoted = v.finish_work(work)
        if promoted is not None:
            self.log.event("queue_promoted", verifier=v.vid,
                           lease=promoted.lease_id, work_kind=promoted.kind,
                           source=promoted.source, via="riven_pending")
        else:
            # Nothing promoted (this was the ahead slot, or it landed alone):
            # the slot-next pointer still names this lease, and would go on
            # showing a finished work as the next task until a fetch repaints.
            self.clear_slot_next(v, work)

    async def _release_pending_riven(self, vid: str | None, lease: str) -> None:
        """Give the work back when its answer will never be taken."""
        v, work = await self._pending_riven_work(vid, lease)
        if work is not None:
            # `flush_pending=False`: this IS the give-up path — trying again here
            # would loop back into it.
            await self._give_back_work(v, work, "riven answer not taken",
                                       flush_pending=False)

    async def _upload_riven(self, slot: int, v: Verifier, work, task: WorkTask,
                            res: dict, ms: int, began: float) -> None:
        if not await self._ensure_queue(v):
            self.on_event("queue", f"{v.label} 队列兜底超时（紫卡），仍按原流程上交")
        payload = self._riven_upload_payload(v, work, res, ms)
        self.log.task(work.lease_id, upload_request=payload,
                      upload_endpoint=f"/api/appraise/{work.code}/result")
        # KEPT IN MEMORY BEFORE THE WIRE.  If this send dies, the kept answer
        # is re-sent by `_retry_pending_riven` while the lease is still ours.
        self._keep_pending_riven(v, work, payload)
        self.riven_sending.add(work.lease_id)
        try:
            out = await self._io(self.remote.appraise_result, work.code, payload)
            self.log.task(work.lease_id, upload_response=out, upload_ms=ms)
        except Exception as e:
            self.log.task(work.lease_id, upload_exception=f"{type(e).__name__}: {e}")
            # KEPT, NOT GIVEN BACK: the lease is what makes a re-send land, and
            # the heartbeat renews a held row, so the answer is still takeable.
            # The retry loop finishes or releases it.
            self.on_event("error", f"{v.label} upload failed: {e}；答案留内存，稍后自动重发")
            self._keep_unconfirmed_answer(v, work, task, res, ms, slot)
            return
        finally:
            self.riven_sending.discard(work.lease_id)
        if out.get("first") is False:
            # NOT A LOSS.  `first` is whether THIS call took the answer; a
            # successful answer clears the lease, so our own retry reads false
            # even though the result was recorded (worker/appraise.js
            # `volunteerAnswer` -> facts().answer).  Keep it; the pending
            # re-send settles it with `appraise/mine`.
            self._keep_unconfirmed_answer(v, work, task, res, ms, slot)
            return
        if out.get("ok"):
            self._drop_pending_riven(work.lease_id)
            self._accept_riven_upload(v, work, task, res, ms, began)
            self._slot_result(slot, v, task, True)
            self.riven_slots.discard(slot)
            return
        if self._riven_answer_retryable(out):
            # A 5xx / 429 / transport fault is not the server refusing the
            # answer; keep it in memory and let the ladder deliver it.
            self.on_event("error", f"{v.label} 上传暂时失败，答案留内存，稍后自动重发")
            self._keep_unconfirmed_answer(v, work, task, res, ms, slot)
            return
        # A REFUSAL IS FINAL (a bad answer, or an appraisal the server no longer
        # keeps): keeping it would only send it again for nothing.
        self._drop_pending_riven(work.lease_id)
        self._slot_result(slot, v, task, False)
        self.task_status.mark_work_failure(v.vid, work, "upload_refused")
        await self._give_back(v, "upload refused")
        self.riven_slots.discard(slot)

    def _keep_unconfirmed_answer(self, v: Verifier, work, task: WorkTask,
                                 res: dict, ms: int, slot: int) -> None:
        """The answer is not confirmed as taken yet: keep it and keep the lease.

        The result stays on the task (the retry that lands closes the work) and
        the answer stays in memory; the heartbeat keeps the lease alive so a
        later copy can still be recorded.
        """
        # DONE, NOT UPLOADED: the row must stop looking runnable, while
        # `uploaded` keeps meaning "the server took it" (the retry sets it).
        work.record_answer(task, res, ms)
        v.sync_state("riven answer kept")
        self.log.event("riven_answer_unconfirmed", verifier=v.vid,
                       lease=work.lease_id, code=work.code)
        self._slot_result(slot, v, task, False)
        self.riven_slots.discard(slot)

    def _accept_riven_upload(self, v: Verifier, work, task: WorkTask,
                             res: dict, ms: int, began: float) -> None:
        """The server took this answer: count it once and close the work."""
        v.record_task_answer(work, task, res, ms)
        self.stats["uploaded"] += v.mark_uploaded(work, [task])
        self.task_status.mark_submitted(
            v.vid, task, int((res or {}).get("work") or 0), time.time())
        promoted = v.finish_work(work)
        if promoted is not None:
            self.log.event("queue_promoted", verifier=v.vid,
                           lease=promoted.lease_id,
                           work_kind=promoted.kind,
                           source=promoted.source, via="riven_done")
        # The yardstick for the NEXT riven's ahead request.
        self.last_riven_ms = max(1.0, (time.time() - began) * 1000.0)
        self.on_event("upload", f"{v.label} -> riven ok "
                                f"builds={res.get('builds', 0)} "
                                f"fights={res.get('fights', 0)} "
                                f"({time.time()-began:.1f}s)")

    async def _upload(self, slot: int, v: Verifier, work, task: WorkTask,
                      res: dict, began: float) -> None:
        """Record one completed row; board packs upload once, at the end."""
        if work is None or work.aborted:
            self._slot_result(slot, v, task, False)
            return
        ms = int((time.time() - began) * 1000)
        if task.kind == "board":
            await self._upload_board(slot, v, work, task, res, ms)
            return
        await self._upload_riven(slot, v, work, task, res, ms, began)

    def _set_slot_next(self, slot: int, v: Verifier) -> None:
        self._slot_next_fields(self.slots[slot], v, v.queued)

    def _begin_slot_view(self, slot: int, v: Verifier, work,
                         task: WorkTask) -> float:
        began = time.time()
        st = self.slots[slot]
        st.verifier, st.kind, st.lease = v.vid, task.kind, work.lease_id
        st.source = work.source
        st.pack_lease = work.lease_id
        st.question = task.question
        st.task_index = task.index + 1
        st.task_total = task.total
        st.lanes = int(self.lanes_for_task(slot, task))
        st.label, st.started_at = task.label(), began
        st.started_mono = time.monotonic()
        self._refresh_slot_next(v)
        if task.kind == "riven_gain":
            req = task.task.get("request") or {}
            st.progress = {
                "kind": "riven_gain",
                "starts": len(req.get("starts") or []),
                "quick": str(req.get("strategy") or "").lower() == "quick",
            }
        else:
            st.progress = {"kind": "board"}
        return began

    def _clear_slot_view(self, slot: int, v: Verifier) -> None:
        st = self.slots[slot]
        st.verifier, st.kind, st.lease, st.source = None, None, None, None
        st.pack_lease, st.question = None, None
        st.task_index, st.task_total = None, None
        st.lanes = 0
        st.label, st.started_at = "", 0.0
        st.started_mono = 0.0
        st.progress = {}
        self._refresh_slot_next(v)

    async def _reserve_slot_task(self, slot: int):
        # RESERVE BEFORE ANY AWAIT.  pick_for_slot and start_task are both
        # synchronous, so the lock makes the pair atomic across the three
        # slot loops.  Renewals stay outside it: they are network calls and
        # must never serialize the compute slots.
        async with self.pick_lock:
            picked = self.pick_for_slot(slot)
            if picked is None:
                return None
            v, work, task = picked
            try:
                v.start_task(work, task)
                if task.manual_priority:
                    self._clear_forced_task()
            except AssertionError:
                # Another slot got there first (legacy race).  It is harmless
                # now: skip this row and let the winner run.
                self.log.event("pick_race", slot=slot, verifier=v.vid,
                               lease=work.lease_id, question=task.question)
                return None
            # A ROW THAT JUMPED THE QUEUE says so once, as it is taken: its age
            # is the reason, so there is nothing to clear later.  The test is
            # `_is_stale_start`, NOT a bare age comparison — with the promotion
            # turned off (limit 0) every row would otherwise read as promoted.
            now = time.time()
            limit = self._stale_start_limit()
            if self._is_stale_start(task, now, limit):
                waited = int(now - task.claimed_at)
                self.log.event("stale_promoted", slot=slot, verifier=v.vid,
                               lease=work.lease_id, question=task.question,
                               waited_s=waited)
                self.on_event("stale", f"{v.label} 等待 {waited // 60} "
                                       f"分钟未开始，提前计算：{task.label()}")
            return v, work, task

    async def _renew_reserved(self, slot: int, v: Verifier, work,
                              task: WorkTask) -> bool:
        if not (task.kind == "board" and self.renew_supported is not False
                and task.row_until
                and task.row_until - time.time() <= RENEW_LEAD_S):
            return True
        if self._sync_source(work) and self._sync_verifier_healthy(v):
            return True               # the sync `held[]` renews this row
        if await self._renew_row(v, work, task):
            return True
        task.lost = True
        self.log.event("row_lost_after_reserve", slot=slot, verifier=v.vid,
                       lease=work.lease_id, question=task.question)
        return False

    async def _run_slot_task(self, slot: int, v: Verifier, work,
                             task: WorkTask, began: float,
                             res: dict | None = None) -> None:
        """Compute one task and run the whole per-task tail (upload, ledger).

        `res` LETS A BATCH HAND ITS RESULTS IN.  2.2a's `/api/board/rows`
        pipelines several rows inside one engine call (each row's serial `score`
        overlapping the next row's fold; measured 3.9% faster, bit-identical),
        and a batch wrapper computes N results in one call and then runs THIS
        tail once per row — unchanged, in row order — so no per-row semantics
        (upload, anchor, ledger, refusal handling) move.  Default `None` is the
        original one-task path.
        """
        st = self.slots[slot]
        try:
            if res is None:
                res = await self._fight(slot, v, work, task)
        except asyncio.CancelledError:
            self.slot_task[slot] = None
            raise
        except RowRefused as e:
            # THE ENGINE REFUSED THIS ROW, not this machine — the board's own
            # validation says no fight can run the build it names.  The
            # reference client leaves such a row to the pack's end and CARRIES
            # ON (69-board-work.js `packRowOnce`); giving the whole lease back
            # here threw the pack's good rows away with it.
            await self._skip_refused_row(slot, v, work, task, e)
        except Exception as e:
            self.on_event("error", f"{v.label} compute failed: {e}")
            if v.work_role(work) is not None:
                # THE SLOT-RECOVERY PATH MAY HAVE RELEASED THIS ROW ALREADY (the
                # engine was stopped under it).  That is not a bad result this
                # machine produced, so it stays out of the 本地无效 tally.
                self.task_status.mark_local_failure(v.vid, task, "compute_failed")
            await self._give_back_work(v, work, "compute failed")
        else:
            st.uploading = v.vid
            st.uploading_lease = work.lease_id
            self._spawn(self._upload(slot, v, work, task, res, began))
        finally:
            self.slot_task[slot] = None
            self._clear_slot_view(slot, v)
            self.stats["compute_s"] += time.time() - began
            if task.kind == "riven_gain":
                self.riven_slots.discard(slot)

    async def _skip_refused_row(self, slot: int, v: Verifier, work, task: WorkTask,
                                e: Exception) -> None:
        """One row the engine refuses is dropped; the rest of the pack runs on.

        `lost` is the state a pack already uses for "not ours to answer"
        (`_renew_reserved`): the row leaves `pending_tasks()`, `all_done()`
        counts it, and `/api/board/answers` hands it back to the pool with
        `done: true` exactly as the page's own refusal does.  It is NOT a local
        failure — this machine did nothing wrong — so it stays out of the
        invalid tally and never costs the pack its other rows.
        """
        task.lost = True
        self.stats["row_refused"] = self.stats.get("row_refused", 0) + 1
        self.log.event("row_refused", slot=slot, verifier=v.vid,
                       lease=work.lease_id, work_kind=task.kind,
                       question=task.question, label=task.label(), error=str(e))
        self.on_event("refused",
                      f"{v.label} 引擎拒绝这一行，已跳过：{task.label()}")
        if work.kind == "board" and task.question and self._board_sync_active():
            # Protocol 8 reports refusals in the next sync (the server may
            # withdraw the row cluster-wide); the row stays dropped locally.
            step = "order" if "order" in str(getattr(e, "endpoint", "")) else "engine"
            self._sync_refused.setdefault(v.vid, []).append(
                {"lease": work.lease_id, "question": task.question,
                 "step": step})
        if work.kind != "board":
            # A riven is one task, so there is no pack to keep going and the
            # lease goes back the way any other failure returns it.
            await self._give_back_work(v, work, "row refused")
            return
        if v.work_all_done(work):
            # NOTHING LEFT TO RUN: say so now.  The rows that were not refused
            # are already answered, and holding the lease until the cutoff
            # would keep this verifier off the queue for nothing.
            await self._flush_board_answers(v, work)

    async def _slot_loop(self, slot: int) -> None:
        """ONE COMPUTE SLOT: take a row, compute it, upload, repeat."""
        idle_since = 0.0            # when this slot last went dry (2.4 reads it)
        while self.running:
            if slot in self.slot_restarting:
                await asyncio.sleep(0.05)
                continue
            picked = await self._reserve_slot_task(slot)
            if picked is not None and slot in self.idle_stopped:
                # 2.4: this slot's engine was reclaimed while it was dry.  Bring
                # it back BEFORE the row is computed — `_reserve_slot_task` only
                # takes the task, it never touches the engine.
                await self._idle_wake(slot)
            if picked is None:
                if self.paused and not self.maintenance:
                    await self._park_slot(slot)
                # How long the slot has sat with nothing to take.  A per-slot
                # dry spell is invisible at the process level — the engine is
                # simply idle — and it is the "发呆" a reader sees.  Only 完整
                # logging PRINTS it; the clock itself is always kept, because
                # 2.4 recycles on it.
                if not idle_since:
                    idle_since = time.time()
                recycle_s = self._cfg_int("idle_recycle_s", 0)
                if (recycle_s > 0 and not self.paused and not self.maintenance
                        and time.time() - idle_since >= recycle_s):
                    await self._idle_release(slot)
                    idle_since = time.time()    # do not retry every 20 ms
                await asyncio.sleep(0.02)
                continue
            if idle_since:
                if self.log.full:
                    self._log_slot_idle(slot, time.time() - idle_since)
                idle_since = 0.0
            v, work, task = picked
            # The task is already inflight/reserved.  A failed renewal now
            # marks that reservation lost instead of racing another slot.
            if not await self._renew_reserved(slot, v, work, task):
                continue
            if not work.started_at:
                work.started_at = time.time()
            # Claim the slot before yielding to the riven assignment, so the
            # idle scanner cannot release it during engine startup.
            self.slot_task[slot] = v.vid
            if task.kind == "riven_gain":
                self.riven_slots.add(slot)
            else:
                self.riven_slots.discard(slot)
            began = self._begin_slot_view(slot, v, work, task)
            want = self._cfg_int("board_batch_rows", 1)
            if task.kind == "board" and want > 1:
                extra = await self._take_slot_batch(slot, v, work, want - 1)
                if extra:
                    await self._run_slot_batch(slot, v, work, task, began, extra)
                else:
                    await self._run_slot_task(slot, v, work, task, began)
            else:
                await self._run_slot_task(slot, v, work, task, began)

    async def _run_slot_batch(self, slot: int, v: Verifier, work, task: WorkTask,
                              began: float, extra: list) -> None:
        """N board tasks in ONE engine call, then the SAME tail once per row.

        WIRED BEHIND `board_batch_rows` (default 1 = OFF, so nothing changes
        until the field says so).  The slot loop is built around ONE task at a
        time — `slot_task`, `st.uploading`, `_begin_slot_view`/`_clear_slot_view`
        are single-task invariants — so the batch never touches them: it computes
        N results in one call and then calls `_run_slot_task(..., res=r)` once
        per row, IN ORDER, which is the identical tail (upload, anchor, ledger,
        refusal handling).

        WHAT IT BUYS: the engine's `/api/board/rows` overlaps each row's serial
        `score` (measured 6-20 ms on a 0.47-2.44 s row, 1.2-4.3%) with the next
        row's fold, folds staying strictly serial.  30 rows in batches of 8
        measured **3.9% faster**, bit-identical (0/30).

        PER-ROW FAILURES STAY PER-ROW: a `RowRefused` entry skips that row only
        (`_skip_refused_row`), anything else marks that row's local failure and
        gives the work back — exactly what the single-task path does.
        """
        tasks = [task] + list(extra)
        tags = [self._log_fight_start(slot, v, t, work) for t in tasks]
        began_batch = time.time()
        try:
            results = await self._fight_board_batch(slot, v, tasks, work, tags)
        except Exception as e:
            # The batch itself failed (engine gone, both rows unfit): fall back
            # to the one-at-a-time path so a batch bug can never lose work.
            self.log.event("board_batch_failed", slot=slot, verifier=v.vid,
                           lease=work.lease_id, rows=len(tasks),
                           error=f"{type(e).__name__}: {e}")
            for t in tasks:
                await self._run_slot_task(slot, v, work, t, began)
            return
        #: ONE record per batch, not per row: this is the field's evidence that
        #: the batched door is actually being used, and how wide it went, for
        #: the cost of a single line per 8 rows.
        self.log.event("board_batch", slot=slot, verifier=v.vid,
                       lease=work.lease_id, rows=len(tasks),
                       ms=round((time.time() - began_batch) * 1000.0, 1))
        for t, r in zip(tasks, results):
            if isinstance(r, Exception):
                if isinstance(r, RowRefused):
                    await self._skip_refused_row(slot, v, work, t, r)
                else:
                    self.on_event("error", f"{v.label} compute failed: {r}")
                    self.task_status.mark_local_failure(v.vid, t, "compute_failed")
                    await self._give_back_work(v, work, "compute failed")
                continue
            await self._run_slot_task(slot, v, work, t, began, res=r)

    async def _run_one_reserved(self, slot: int, v: Verifier, work,
                                task: WorkTask) -> None:
        """THE SLOT LOOP'S OWN PRELUDE, for a task taken OUT OF BAND.

        `_reserve_slot_task` has no counterpart that gives a task back: once
        `v.start_task` has run, the row must be computed or it stays reserved
        forever.  So every task this class reserves is run — this is the exact
        prelude the slot loop performs before `_run_slot_task`, kept in one place
        so an out-of-band task is handled identically.
        """
        if not await self._renew_reserved(slot, v, work, task):
            return
        if not work.started_at:
            work.started_at = time.time()
        self.slot_task[slot] = v.vid
        if task.kind == "riven_gain":
            self.riven_slots.add(slot)
        else:
            self.riven_slots.discard(slot)
        began = self._begin_slot_view(slot, v, work, task)
        await self._run_slot_task(slot, v, work, task, began)

    async def _take_slot_batch(self, slot: int, v: Verifier, work,
                               want: int) -> list:
        """Reserve up to `want` MORE board tasks from the SAME pack.

        ONLY same-lease board rows are collected: a batch is computed against
        one `work` (its lease, its log tag, its upload), so anything else is run
        immediately through `_run_one_reserved` rather than folded in — and run
        it we must, because a reservation cannot be handed back.
        """
        extra = []
        while len(extra) < want:
            picked = await self._reserve_slot_task(slot)
            if picked is None:
                break
            v2, work2, t2 = picked
            same = (t2.kind == "board" and v2.vid == v.vid
                    and work2.lease_id == work.lease_id)
            if same:
                extra.append(t2)
                continue
            # NOT OURS: compute it the ordinary way and stop collecting.
            await self._run_one_reserved(slot, v2, work2, t2)
            break
        return extra

    async def _slot_supervisor(self, slot: int) -> None:
        """Keep one compute slot alive across unexpected Python exceptions."""
        delay = 0.5
        while self.running:
            began = time.time()
            try:
                await self._slot_loop(slot)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.on_event("error", f"计算进程 {slot + 1} 异常: {err}")
                self.log.event("slot_loop_error", slot=slot,
                               port=self.engines[slot].port, error=err)
                await self._recover_slot(slot, "slot_loop_error")
                if time.time() - began > 60.0:
                    delay = 0.5
                await asyncio.sleep(delay)
                delay = min(5.0, delay * 2.0)

    def _recover_owner(self, slot: int, v, work):
        if v is None:
            vid = self.slot_task.get(slot)
            v = next((x for x in self.verifiers if x.vid == vid), None) if vid else None
        if work is None and v is not None:
            lease = self.slots[slot].lease
            work = next((w for w, _ in v.works() if w.lease_id == lease), None)
            if work is None:
                work = v.lease
        return v, work

    async def _stop_engine_for_recover(self, slot: int, port: int) -> None:
        try:
            await self._cpu(self.engines[slot].stop)
        except Exception as e:
            self.log.event("slot_recover_stop_error", slot=slot, port=port,
                           error=f"{type(e).__name__}: {e}")

    async def _release_recovered_work(self, slot: int, v, work,
                                      reason: str) -> None:
        if (v is None or work is None or v.work_role(work) is None
                or work.aborted or work.all_done()):
            return
        try:
            await self._give_back_work(v, work, f"{reason} recover")
        except Exception as e:
            self.log.event("slot_recover_release_error", slot=slot,
                           verifier=v.vid, lease=work.lease_id,
                           error=f"{type(e).__name__}: {e}")

    def _clear_recovered_slot(self, slot: int) -> None:
        self.slot_task[slot] = None
        st = self.slots[slot]
        st.verifier = st.kind = st.lease = st.source = None
        st.uploading = None
        st.uploading_lease = None
        st.pack_lease = st.question = None
        st.task_index = st.task_total = None
        st.label, st.started_at = "", 0.0
        st.started_mono = 0.0
        st.progress = {}

    def _apply_recovered_policies(self, slot: int, port: int) -> None:
        try:
            self.apply_cores()
            self.apply_core_type()
        except Exception as e:
            self.log.event("slot_recover_affinity_error", slot=slot,
                           port=port, error=f"{type(e).__name__}: {e}")

    async def _recover_slot(self, slot: int, reason: str, v=None,
                            work=None) -> None:
        """Restart one engine process and clear the slot's stale UI state.

        Called by the supervisor after an exception and by the watchdog after
        a stuck/idle slot.  The engine is stopped first, so a fold already in
        flight cannot keep burning cores while we rebuild the slot.

        The watchdog passes `v`/`work` captured before it cancels the stuck
        supervisor, because that cancellation clears `slot_task` first.
        """
        if slot in self.slot_restarting:
            return
        if slot < 0 or slot >= len(self.engines):
            return
        self.slot_restarting.add(slot)
        port = self.engines[slot].port
        v, work = self._recover_owner(slot, v, work)
        vid = v.vid if v is not None else None
        try:
            self.log.event("slot_recover_begin", slot=slot, port=port,
                           reason=reason, verifier=vid)
            await self._stop_engine_for_recover(slot, port)
            # Return work that was already reserved by the dead/stuck slot.
            await self._release_recovered_work(slot, v, work, reason)
            self._clear_recovered_slot(slot)
            await self._cpu(self.engines[slot].start, self.cpu_limit)
            # Re-apply the user's core selection and P/E/Core-Type policy to the
            # fresh process.  Engine.start attaches the shared CPU-rate job.
            self._apply_recovered_policies(slot, port)
            self.on_event("slot", f"计算进程 {slot + 1} 已重启（{reason}）")
            self.log.event("slot_recover_done", slot=slot, port=port,
                           reason=reason)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self.on_event("error", f"计算进程 {slot + 1} 重启失败: {err}")
            self.log.event("slot_recover_error", slot=slot, port=port,
                           reason=reason, error=err)
        finally:
            self.slot_restarting.discard(slot)

    def _watchdog_settings(self) -> tuple[int, int]:
        try:
            interval = max(2, int(self.cfg.get("slot_watchdog_seconds") or 10))
            restart_after = max(interval,
                                int(self.cfg.get("slot_idle_restart_seconds") or 30))
        except Exception:
            interval, restart_after = 10, 30
        return interval, max(1, (restart_after + interval - 1) // interval)

    def _spawn_supervisor(self, slot: int) -> None:
        fresh = asyncio.create_task(self._slot_supervisor(slot))
        self.slot_supervisors[slot] = fresh
        self.slot_tasks.append(fresh)
        self.slot_idle_count[slot] = 0

    async def _watchdog_supervisor(self, slot: int) -> bool:
        task = self.slot_supervisors.get(slot)
        if task is not None and not task.done():
            return False
        err = "supervisor missing" if task is None else "supervisor exited"
        self.log.event("slot_watchdog_dead", slot=slot,
                       port=self.engines[slot].port, note=err)
        await self._recover_slot(slot, "watchdog_supervisor")
        self._spawn_supervisor(slot)
        return True

    async def _slot_meta_ok(self, slot: int) -> bool:
        # One local health read only when the slot is actually suspect.
        try:
            meta = await self._cpu(self.engines[slot].get, "/api/meta", 2)
            return bool(isinstance(meta, dict) and meta.get("ok", True))
        except Exception as e:
            self.log.event("slot_watchdog_meta_error", slot=slot,
                           port=self.engines[slot].port,
                           error=f"{type(e).__name__}: {e}")
            return False

    async def _watchdog_idle(self, slot: int, need_ticks: int) -> None:
        if self.slot_task.get(slot) is not None:
            self.slot_idle_count[slot] = 0
            return
        try:
            has_work = self.pick_for_slot(slot) is not None
        except Exception:
            has_work = False
        if not has_work:
            self.slot_idle_count[slot] = 0
            return
        self.slot_idle_count[slot] = self.slot_idle_count.get(slot, 0) + 1
        if self.slot_idle_count[slot] < need_ticks:
            return
        meta_ok = await self._slot_meta_ok(slot)
        self.log.event("slot_watchdog_idle", slot=slot,
                       port=self.engines[slot].port,
                       idle_ticks=self.slot_idle_count[slot],
                       meta_ok=meta_ok)
        # Capture the verifier/work BEFORE cancel: the cancellation path
        # clears slot_task in its finally.
        stuck_vid = self.slot_task.get(slot)
        stuck_v = next((x for x in self.verifiers if x.vid == stuck_vid), None)
        stuck_work = None
        if stuck_v is not None:
            lease = self.slots[slot].lease
            stuck_work = next((w for w, _ in stuck_v.works()
                               if w.lease_id == lease), None) or stuck_v.lease
        task = self.slot_supervisors.get(slot)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._recover_slot(slot, "watchdog_idle_pending",
                                 v=stuck_v, work=stuck_work)
        self._spawn_supervisor(slot)

    async def _watchdog_round(self, need_ticks: int) -> None:
        for slot in range(len(self.engines)):
            if slot in self.slot_restarting:
                continue
            if await self._watchdog_supervisor(slot):
                continue
            await self._watchdog_idle(slot, need_ticks)

    async def _slot_watchdog(self) -> None:
        """Detect a dead or wedged slot without touching the network normally."""
        interval, need_ticks = self._watchdog_settings()
        while self.running:
            try:
                if not self.paused and not self.maintenance:
                    await self._watchdog_round(need_ticks)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_event("error", f"slot watchdog: {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    # ------------------------------------------------------ the stall watchdog
    def _slot_stall_settings(self) -> tuple[float, int]:
        try:
            every = max(10.0, float(self.cfg.get("slot_stall_seconds")
                                    or SLOT_STALL_EVERY_S))
        except (TypeError, ValueError):
            every = SLOT_STALL_EVERY_S
        try:
            checks = max(2, int(self.cfg.get("slot_stall_checks")
                                or SLOT_STALL_CHECKS))
        except (TypeError, ValueError):
            checks = SLOT_STALL_CHECKS
        return every, checks

    async def _slot_stall_round(self, checks: int) -> None:
        """Rebuild a slot whose engine has made no CPU headway on its row.

        Idle slots and uploads are not judged; neither is a dead process (the
        call fails on its own) or a slot that changed lease since last time.
        """
        now = time.time()
        for slot in range(len(self.slots)):
            if slot in self.slot_restarting:
                continue
            st = self.slots[slot]
            if self.slot_task.get(slot) is None or st.uploading:
                self.slot_cpu_sample.pop(slot, None)
                continue
            engine = self.engines[slot] if slot < len(self.engines) else None
            proc = getattr(engine, "proc", None)
            if engine is None or proc is None or proc.poll() is not None:
                self.slot_cpu_sample.pop(slot, None)
                continue
            cpu = cores.process_cpu_seconds(proc.pid)
            if cpu is None:
                self.slot_cpu_sample.pop(slot, None)
                continue
            lease = st.lease or ""
            prev = self.slot_cpu_sample.get(slot)
            if (prev is None or prev[0] != lease
                    or cpu - prev[1] > SLOT_STALL_CPU_EPS):
                self.slot_cpu_sample[slot] = (lease, cpu, now, 0)
                continue
            count = prev[3] + 1
            self.slot_cpu_sample[slot] = (lease, prev[1], prev[2], count)
            if count < checks:
                continue
            self.log.event("slot_stall", slot=slot, port=engine.port, lease=lease,
                           work_kind=st.kind, label=st.label, checks=count,
                           cpu_seconds=round(cpu, 2),
                           on_row_s=int(max(0.0, now - (st.started_at or now))))
            self.on_event("slot", f"计算进程 {slot + 1} 连续 {count} 次没有任何 CPU 进展，"
                                  f"判定卡住并重启：{st.label}")
            self.slot_cpu_sample.pop(slot, None)
            await self._recover_slot(slot, "compute stall")

    async def _slot_stall_loop(self) -> None:
        every, checks = self._slot_stall_settings()
        while self.running:
            try:
                if not self.paused and not self.maintenance:
                    await self._slot_stall_round(checks)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_event("error", f"slot stall watchdog: {type(e).__name__}: {e}")
            await asyncio.sleep(every)

    # -------------------------------------------------- the verifier watchdog
    def _verifier_watchdog_seconds(self) -> float:
        try:
            return max(1.0, float(self.cfg.get("verifier_watchdog_seconds")
                                  or VERIFIER_WATCHDOG_S))
        except (TypeError, ValueError):
            return VERIFIER_WATCHDOG_S

    #: A riven lost inside this many seconds of another one counts towards the
    #: cooldown; three in a row and the riven door is shut for `RIVEN_COOLDOWN_S`.
    RIVEN_LOSS_WINDOW_S = 900.0
    RIVEN_LOSSES_MAX = 3
    RIVEN_COOLDOWN_S = 600.0

    def _note_riven_lost(self, now: float | None = None) -> None:
        """Shut the riven door after a run of leases that keep being taken.

        A riven costs minutes of a verifier and produces nothing when its lease
        is revoked mid-search.  Measured: one verifier lost two in a row and
        spent 00:38-00:40 producing nothing while the other two carried the
        board.  A short refusal is cheaper than a third wasted search — and the
        server hands a refused riven straight back into the pool.
        """
        now = time.time() if now is None else float(now)
        window = now - self.RIVEN_LOSS_WINDOW_S
        self._riven_lost_at = [t for t in self._riven_lost_at if t >= window]
        self._riven_lost_at.append(now)
        if len(self._riven_lost_at) < self.RIVEN_LOSSES_MAX:
            return
        self._riven_refuse_until = now + self.RIVEN_COOLDOWN_S
        self._riven_lost_at = []
        self.log.event("riven_cooldown", until=self._riven_refuse_until,
                       seconds=self.RIVEN_COOLDOWN_S)
        self.on_event("deadline", f"紫卡租约连续被收走 "
                                  f"{self.RIVEN_LOSSES_MAX} 次，"
                                  f"{int(self.RIVEN_COOLDOWN_S // 60)} 分钟内不再接紫卡")

    def _riven_wanted(self, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        return now >= float(self._riven_refuse_until or 0.0)

    @staticmethod
    def _work_age_s(work) -> float | None:
        """How long ago this work arrived (None when it cannot be known).

        `deadline` is the arrival plus `lease_ms` minus a safety margin, so the
        arrival time is recoverable — and knowing the age of a lost lease is
        the only way to tell "the lease is short" from "our beat is late".
        """
        try:
            deadline = float(getattr(work, "deadline", 0.0) or 0.0)
            lease_ms = float(getattr(work, "lease_ms", 0) or 0)
        except (TypeError, ValueError):
            return None
        if deadline <= 0 or lease_ms <= 0:
            return None
        # `deadline = arrival + max(30 s, lease_ms - 30 s)` (work_model).
        span = max(BOARD_LEASE_SAFE_S, lease_ms / 1000.0 - BOARD_LEASE_SAFE_S)
        return round(time.time() - (deadline - span), 1)

    def _board_kept_owed(self, v: Verifier) -> int:
        """This id's kept answers that still owe a `taken` verdict.

        Only entries whose lease this verifier still holds: a kept answer
        under a lease already released can never land, and counting it would
        keep the verifier "not wedged" for the whole two-hour TTL.
        """
        leases = {w.lease_id for w, _ in v.works() if w is not None}
        if not leases:
            return 0
        return sum(1 for (lease, _q), e in self._board_kept.items()
                   if e.get("vid") == v.vid and lease in leases)

    async def _close_finished_sync_packs(self, v: Verifier) -> int:
        """Close protocol-8 packs that are computed and fully accounted for.

        A sync pack has no `/answers` tail to close it: a finished row leaves
        `held[]`, the answer rides `results`, and `taken`/`dropped` is the
        receipt.  Nothing closed the work locally, so two all-done packs sat
        on the verifier until the watchdog gave them back — which is also how
        an answer still in flight got its lease pulled out from under it.
        Closing here means the reset below only ever sees a real wedge.
        """
        closed = 0
        for work, _role in list(v.works()):
            if work is None or work.aborted or work.kind != "board":
                continue
            if not self._sync_source(work) or not v.work_all_done(work):
                continue
            if any(k[0] == work.lease_id for k in self._board_kept):
                continue              # answers still owed: the sync owes them
            self._finish_board_pack(v, work)
            closed += 1
        if closed:
            self.log.event("sync_pack_closed", verifier=v.vid, packs=closed)
        return closed

    def _wedged_verifier(self, v: Verifier, now: float) -> bool:
        """True when this verifier can no longer ask for anything.

        Both roads are closed: a plain ask needs `IDLE`, an `ahead` ask needs a
        live lease.  A work that is finished or aborted is neither — the server
        takes an answer only under a live lease — and a fetch that ended
        without a verdict leaves the FSM in `FETCHING` for ever.  Busy is not
        wedged: an engine on its row, an upload in flight, a fetch on the wire
        and a finished answer still waiting to be delivered all keep it alone.
        """
        if v.claim_pending:
            return False
        task = self.fetch_tasks.get(v.vid)
        if task is not None and not task.done():
            return False              # a fetch is legitimately in flight
        # `fetching_ahead` with nothing on the wire is the leak, not business.
        works = [w for w, _ in v.works()]
        if any(self._riven_pending_exists(w.lease_id) for w in works):
            return False              # an undelivered answer keeps its lease
        # A BOARD PACK THAT IS ALL DONE BUT STILL OWES ANSWERS IS NOT WEDGED:
        # its rows left `held[]`, its answers are in `_board_kept` waiting for
        # the next sync's `results`.  Resetting it here released the lease the
        # answers needed, so finished work came back as "退回" — every one of
        # the night's watchdog resets was this case.
        if self._board_kept_owed(v):
            return False              # answers waiting for a `taken` verdict
        if any(self._slot_for_work(v, w) is not None for w in works):
            return False              # computing or uploading right now
        if any(w.is_live(now) for w in works):
            return False              # holds something it can still work on
        if v.state is State.IDLE and not works:
            return False              # a normal idle verifier asks next round
        if v.state is State.COOLING:
            return False              # the short cool-off after an empty ask
        return True

    async def _reset_wedged_verifier(self, v: Verifier) -> None:
        works = [w for w, _ in v.works()]
        self.log.event("verifier_watchdog", verifier=v.vid, state=v.state.value,
                       fetching_count=self.fetching_count,
                       works=[(w.lease_id, w.kind, w.aborted, w.all_done())
                              for w in works])
        self.on_event("watchdog", f"{v.label} 看门狗：状态卡住"
                                  f"（{v.state.value} 且手上没有可用的任务），"
                                  f"已复位并重新申请")
        for work in works:
            # `_give_back_work` flushes a finished answer first, so a reset can
            # never throw one away; a work the server already closed costs one
            # harmless release call.
            try:
                await self._give_back_work(v, work, "watchdog reset")
            except Exception as e:
                self.log.event("verifier_watchdog_error", verifier=v.vid,
                               lease=work.lease_id,
                               error=f"{type(e).__name__}: {e}")
        v.cancel_fetch("watchdog reset")
        if v.lease is not None:
            v.give_back("watchdog reset")
        v.sync_state("watchdog reset")

    async def verifier_watchdog_round(self) -> None:
        if self.paused or self.maintenance:
            return
        now = time.time()
        for v in list(self.verifiers):
            # CLOSE WHAT IS FINISHED BEFORE CALLING ANYTHING WEDGED: a
            # protocol-8 pack whose answers have all been accounted for is
            # simply done, and handing it back would only cost a release and
            # a slot.  A pack that still owes answers stays (and is not
            # wedged) — the sync loop delivers those.
            try:
                await self._close_finished_sync_packs(v)
            except Exception as e:
                self.log.event("verifier_watchdog_error", verifier=v.vid,
                               error=f"{type(e).__name__}: {e}")
            if self._wedged_verifier(v, now):
                await self._reset_wedged_verifier(v)

    async def verifier_watchdog_loop(self, every: float | None = None) -> None:
        """Every ten seconds, free a verifier that can no longer ask."""
        interval = float(every) if every else self._verifier_watchdog_seconds()
        while self.running:
            try:
                await self.verifier_watchdog_round()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_event("error",
                              f"verifier watchdog: {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------ the devices
    async def _cancel_all_fetches(self) -> None:
        # FORCE-CANCEL every fetch.  The underlying HTTP request may still
        # finish in its worker thread, but its result is discarded.
        pending = [t for t in self.fetch_tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            self.on_event("refresh", f"cancelled {len(pending)} in-flight fetch(es)")
            await asyncio.gather(*pending, return_exceptions=True)
        self.fetch_tasks.clear()

    async def _return_all_for_refresh(self) -> None:
        for v in self.verifiers:
            v.cancel_fetch("refreshing verifier ids")
            if v.lease or v.queued:
                await self._give_back_all(v, "refreshing verifier ids")

    async def _make_fresh_verifiers(self, n: int) -> list:
        # DELAYED CLAIM applies to refreshed ids too.
        fresh = []
        for i in range(n):
            fresh.append(Verifier(await self.fresh_unclaimed_verifier(),
                                  label=f"v{i+1:02d}"))
        return fresh

    def _store_refreshed_verifiers(self, fresh: list) -> None:
        self.verifiers = fresh
        self.cfg["verifiers"] = [v.vid for v in fresh]
        self.cfg["max_verifiers"] = len(fresh)
        self.cfg["active_verifiers"] = min(int(self.cfg["active_verifiers"]),
                                           len(fresh))
        self.cfg["base_verifiers"] = min(int(self.cfg.get("base_verifiers") or 6),
                                         len(fresh))
        self.cfg["claimed"] = []          # filled one-by-one after 2 tasks
        try:
            self.on_config_changed()
        except Exception:
            pass

    async def refresh_verifiers(self, n: int | None = None) -> int:
        """NEW IDS FOR THIS MACHINE.

        A verifier id IS a device — the thing the server leases work to, and the
        thing it refuses twice.  Copying this client to another machine and
        starting it on the same ids makes the server see ONE device asking from
        two places: it hands the work to whichever asks first and the other is
        given nothing.  So a copy has to refresh.

        Refresh now pauses fetching first.  The previous version refused whenever
        any verifier was in a long `FETCHING` call; on the current slow server
        that could be almost always, so the button looked like it did nothing.
        """
        n = int(n or int(self.cfg["max_verifiers"]) or len(self.verifiers))
        was_maintenance = self.maintenance
        self.maintenance = True                 # stop starting new fetches
        try:
            await self._cancel_all_fetches()
            await self._return_all_for_refresh()
            fresh = await self._make_fresh_verifiers(n)
            self._store_refreshed_verifiers(fresh)
            self.on_event("refresh", f"{len(fresh)} fresh verifier ids created "
                                     f"(delayed claim after current+ahead)")
            return len(fresh)
        finally:
            self.maintenance = was_maintenance

    def apply_cores(self) -> None:
        """Restrict the engine processes to the cores the page selected.

        Applied to the PROCESS, so every thread of the engine stays inside the
        mask.  An EMPTY selection means all cores and is now applied as such —
        it used to call `pin(None, [])`, which had no pid and cleared nothing,
        so a pin could only be undone by restarting the client.  The client's
        own log-task thread is never in the pool: see `_log_cpu`.
        """
        cpus = self._engine_core_pool()
        failed = []
        for e in self.engines:
            if not e.proc:
                continue
            if not cores.pin(e.proc.pid, cpus):
                failed.append(e.port)
        self.on_event("cores", f"引擎 CPU 限制 -> {self.core_pool_label()}"
                      + (f"（{len(failed)} 个引擎设置失败）" if failed else ""))

    # ------------------------------------------- the client's own CPU home
    def _log_cpu(self) -> dict | None:
        """This client's home logical processor under the log-task policy.

        The plan follows the config value: the client process is pinned to it
        at start-up (app.py), and every engine pool below keeps that one
        thread out, so Python's loop, its pools and its logger always have a
        processor the engines can never take.  Re-read when the value changes
        (settings or a config reload), not on every call.
        """
        mode = cores.log_cpu_mode(self.cfg)
        if getattr(self, "_log_cpu_key", None) != mode:
            self._log_cpu_cache = cores.log_cpu_plan(self.cfg, refresh=True)
            self._log_cpu_key = mode
        return self._log_cpu_cache

    def _refresh_log_cpu(self) -> None:
        """Force a re-read after a settings change (does not re-pin)."""
        self._log_cpu_key = None
        self._log_cpu_cache = cores.log_cpu_plan(self.cfg, refresh=True)

    def _deliver_lock(self, vid: str) -> asyncio.Lock:
        """ONE WRITER PER VERIFIER for work delivery.

        Two doors fill the same two FSM slots — the protocol-8 sync reply's
        pack and the classic `/work` ahead fetch.  Both run on the event loop,
        so the danger is not a torn write but an interleaving across an await:
        this lock makes "check the slots, fill one, release the extra" one
        step, so a pack can never land in a slot another door just took.
        """
        lock = self._deliver_locks.get(vid)
        if lock is None:
            lock = asyncio.Lock()
            self._deliver_locks[vid] = lock
        return lock

    def _engine_core_pool(self) -> list[int]:
        """Logical CPUs the engine affinity masks may use: pool minus home.

        With a log-task home an EMPTY `engine_cores` no longer means "the
        whole machine": the client's own thread comes out, or an engine could
        be scheduled on the one logical processor Python is pinned to and the
        guarantee would be gone.  The sibling of an SMT core stays in — that
        is the half of the core compute is meant to have.
        """
        cpus = [int(c) for c in (self.cfg.get("engine_cores") or [])]
        plan = self._log_cpu()
        if plan is None:
            return cpus
        if not cpus:
            cpus = [int(c.get("index", -1)) for c in cores.cpu_sets()]
            if not cpus or -1 in cpus:
                cpus = cores.all_cpus()
        return [c for c in cpus if c != int(plan["logical"])]

    def _engine_selected(self) -> list[int]:
        """What the CPU-Set schedulers may hand out: ids, or logical indices.

        With a log-task home the schedulers get the reduced pool even when
        `engine_cores` is empty — "all cores" no longer includes the client's
        own thread, so a balanced plan never hands an engine the core Python
        is pinned to (its sibling stays in the pool, which is the point).
        """
        plan = self._log_cpu()
        cfg_cpus = [int(c) for c in (self.cfg.get("engine_cores") or [])]
        if plan is None:
            return cfg_cpus
        if cfg_cpus:
            return [c for c in cfg_cpus if c != int(plan["logical"])]
        return [int(i) for i in plan["compute_ids"]]

    def _attempt_plan(self, label: str, fn):
        """One rung of the fallback ladder; None means "try the next one"."""
        try:
            groups, primary, note = fn()
        except Exception as e:
            self.on_event("cores", f"{label}失败：{type(e).__name__}: {e}")
            return None
        if groups and all(groups):
            return groups, primary, note
        return None

    @staticmethod
    def _grouped_plan(n: int, selected):
        groups, note = cores.schedule_groups(n, selected)
        return groups, [], note

    @staticmethod
    def _even_plan(n: int, selected):
        groups, note = cores.schedule_even(n, selected)
        return groups, [], note

    def _core_plan(self, n: int, selected) -> tuple[list, list, str]:
        """Balanced P/E, then LLC groups, then a whole-core even split.

        Multi-tier machines are where a grouping mistake costs the most, so a
        plan that fails its own check is never handed to the engines: the
        fallback ladder ends in `schedule_even`, which hands out WHOLE physical
        cores so no two slots ever share a core's sibling threads.
        """
        plan = self._attempt_plan(
            "均衡分配", lambda: cores.schedule_balanced(n, selected))
        if plan:
            return plan
        plan = self._attempt_plan(
            "分组分配", lambda: self._grouped_plan(n, selected))
        if plan:
            self.on_event("cores", "均衡分配不可用，回退到按 LLC/类别分组")
            return plan
        plan = self._attempt_plan(
            "完全平均分", lambda: self._even_plan(n, selected))
        if plan:
            self.on_event("cores", plan[2] or "回退：完全平均分")
            return plan
        self.on_event("cores", "所有分组都失败，交给系统调度")
        return [], [], ""

    @staticmethod
    def _plan_primary(primary, i: int, ids, sets) -> int | None:
        """The plan's primary core for slot `i`, or the slot's best core."""
        if primary and i < len(primary) and primary[i] is not None:
            return int(primary[i])
        return cores.best_set_id(ids, sets)

    def _store_core_plan(self, groups, primary) -> None:
        """Remember each slot's CPU Sets, logical CPUs and fastest core."""
        sets = cores.cpu_sets()
        self.slot_cpu_sets = groups
        self.slot_cpus = [cores.logical_of_set_ids(g, sets) for g in groups]
        self.slot_primary_sets = []
        self.slot_primary = []
        for i, ids in enumerate(groups):
            pid = self._plan_primary(primary, i, ids, sets)
            self.slot_primary_sets.append(pid if pid is not None else -1)
            logical = cores.logical_of_set_ids([pid], sets) if pid is not None else []
            if not logical and self.slot_cpus[i]:
                logical = [self.slot_cpus[i][0]]
            self.slot_primary.append(logical[0] if logical else -1)

    def _restore_slot_affinity(self, slot: int) -> None:
        """Back to the whole engine pool, then this slot's CPU Sets again."""
        e = self.engines[slot]
        if e.proc is None:
            return
        e.pin_affinity(self._engine_core_pool())
        ids = self.slot_cpu_sets[slot] if slot < len(self.slot_cpu_sets) else []
        if ids:
            e.set_cpu_sets(ids)

    async def _cpu_on_primary(self, slot: int, call):
        """ONE-LANE work runs on a WHOLE physical core of this slot.

        A call with a single lane has nothing to spread across cores, so the
        fastest thing to do with it is give it an entire physical core: the
        primary core's SMT sibling is pinned in too, so the serial work (the
        order/fold/score tail and every one-lane riven) uses BOTH hardware
        threads of one core instead of half of it while a neighbour thread
        fights for the other half.  A multi-lane call is left alone: pinning it
        would throw its lanes away.
        """
        primary = self.slot_primary[slot] if slot < len(self.slot_primary) else -1
        # The affinity mask is 64 bits and group-scoped, so a CPU in another
        # processor group cannot be pinned this way; leave it to the CPU Sets.
        if primary is None or primary < 0 or primary >= 64:
            return await call()
        # NEVER take the client's own home thread, even as a sibling: a primary
        # on the core Python is pinned to would otherwise hand this engine the
        # one logical processor the whole log-cpu policy exists to reserve.
        home = self._log_cpu()
        home = int(home["logical"]) if home else -1
        kin = [c for c in cores.core_siblings(primary) if c < 64 and c != home]
        if not self.engines[slot].pin_affinity(kin):
            return await call()
        try:
            return await call()
        finally:
            self._restore_slot_affinity(slot)

    def _set_engine_cpu_sets(self, e, ids) -> None:
        """Apply one engine's CPU Sets; a clear is silent, a real set is not."""
        if not e.proc:
            return
        if not e.set_cpu_sets(ids) and ids:
            self.on_event("cores", f":{e.port} CPU 集合设置失败")

    def _set_engine_eco(self, e, slow: bool) -> None:
        if e.proc:
            e.set_power_throttling(slow)

    def _apply_max_core_sets(self) -> None:
        """One balanced P/E CPU Set list per engine, plus the primary core."""
        n = self.slot_count()
        selected = self._engine_selected()
        groups, primary, note = self._core_plan(n, selected)
        self._store_core_plan(groups, primary)
        self.core_type_note = note
        for i, e in enumerate(self.engines):
            self._set_engine_cpu_sets(e, groups[i] if i < len(groups) else [])
        for e in self.engines:
            self._set_engine_eco(e, False)
        label = cores.core_type_label("max")
        self.on_event("cores", f"核心类型 {label}：{self.core_type_note or ''}")
        self._log_sched_plan("max", groups)

    def _apply_class_core_sets(self, ct: str) -> None:
        """`auto`, `p` or `e`: one CPU Set list for every engine."""
        ids, note = cores.cpu_set_ids(ct)
        self.core_type_note = note
        use = ids if ct not in ("auto",) and ids else []
        plan = self._log_cpu()
        if use and plan is not None:
            # Never hand an engine the core this client is pinned to — the
            # "e" choice in particular would otherwise park a compute thread
            # on the client's own SMT sibling's core-mate.
            use = [i for i in use if i != int(plan["set_id"])]
        for e in self.engines:
            self._set_engine_cpu_sets(e, use)
        # `p` asks Windows not to slow a background process down; `e` asks it
        # to.  The same pair simfleet uses.
        if ct in ("p", "e"):
            for e in self.engines:
                self._set_engine_eco(e, ct == "e")
        # REMEMBER THE SLOT SHARES so `_restore_slot_affinity` re-applies them
        # after a one-lane pin (it reads `slot_cpu_sets`), and so a single-lane
        # call still lands on the fastest AVAILABLE core in every mode, not
        # only `max`.
        self.slot_cpu_sets = [list(use) for _ in self.engines]
        self._store_serial_primary([list(use) for _ in self.engines])
        suffix = "（对新开的计算线程生效）" if (ct in ("p", "e") and ids) else ""
        self.on_event("cores", f"核心类型 {cores.core_type_label(ct)}：{note}{suffix}")
        self._log_sched_plan(ct, [use] * len(self.engines))

    def _store_serial_primary(self, groups) -> None:
        """Pick the core a ONE-LANE call should use, per slot.

        The fastest core is the HIGHEST EfficiencyClass (a P-core on an Intel
        hybrid — Windows documents a higher class as faster), and among those
        the lowest logical index, which is the part's favoured/top-turbo core.
        A slot with no set list of its own (auto mode) falls back to the whole
        engine pool.  `_cpu_on_primary` pins to this core's SMT siblings so a
        serial tail gets a whole physical PERFORMANCE core, never an E-core.
        """
        sets = cores.cpu_sets()
        home = self._log_cpu()
        home_id = int(home["set_id"]) if home else -1
        self.slot_primary = []
        for i, e in enumerate(self.engines):
            ids = list(groups[i]) if groups and i < len(groups) else []
            if not ids:
                ids = [int(c["id"]) for c in sets if int(c["id"]) != home_id]
            if not ids:
                self.slot_primary.append(-1)
                continue
            best = cores.best_set_id(ids, sets)
            logical = cores.logical_of_set_ids([best], sets) if best is not None else []
            self.slot_primary.append(logical[0] if logical else -1)

    def _log_sched_plan(self, ct: str, groups) -> None:
        """完整 ONLY: what each engine's CPU scheduling ACTUALLY ended up as.

        `apply_cores` writes a HARD affinity mask (pool minus the client's
        home) and `apply_core_type` writes a SOFT CPU Set preference the
        engine's worker threads inherit.  Both are read back from the live
        process here, so a plan that was computed but never landed — an empty
        Set on a machine that was meant to be split, a mask wider than the
        pool — is visible rather than assumed.  Nothing is logged unless the
        level is 完整.
        """
        if not self.log.full:
            return
        pool = self._engine_core_pool()
        plan = self._log_cpu()
        engines = []
        for i, e in enumerate(self.engines):
            pid = e.proc.pid if e.proc is not None else None
            want = []
            try:
                want = list(groups[i]) if groups and i < len(groups) else []
            except Exception:
                want = []
            engines.append({
                "slot": i, "port": e.port, "pid": pid,
                "affinity_mask": (cores.process_affinity_cpus(pid)
                                  if pid else None),
                "cpu_sets": (cores.process_cpu_set_ids(pid) if pid else None),
                "plan_cpu_sets": want,
            })
        self.log.event(
            "cpu_schedule", core_type=ct, note=self.core_type_note or "",
            engine_pool=pool, client_home=(plan or {}).get("logical"),
            lanes=list(self.slot_lanes), lanes_per_slot_cfg=self.cfg.get("lanes_per_slot"),
            engines=engines)

    def apply_core_type(self) -> None:
        """Steer engine work onto P-cores, E-cores, CCD groups, or Windows.

        NOT an affinity mask: this is `SetProcessDefaultCpuSets`, and the
        engine's per-batch worker threads inherit it as they are created.  Only
        scheduling changes — the arithmetic and the numbers do not.
        """
        ct = str(self.cfg.get("core_type") or "auto").lower()
        if ct not in cores.CORE_TYPES:
            ct = "auto"
        if ct == "max":
            self._apply_max_core_sets()
            return
        self._apply_class_core_sets(ct)

    def apply_cpu_limit(self) -> None:
        """Change the shared Job Object hard cap on all running engines."""
        try:
            percent = max(10, min(100, int(self.cfg.get("cpu_limit_percent") or 100)))
        except Exception:
            percent = 100
        if self.cpu_limit is None:
            self.cpu_limit = cores.CpuRateLimit(percent)
        ok = self.cpu_limit.set_percent(percent)
        # READ IT BACK.  The console used to print the number out of config, so
        # a job that refused the setting and one that took it looked the same.
        self.cpu_limit_actual = self.cpu_limit.actual_percent()
        if not ok:
            self.on_event("cpu", f"CPU 限速设置失败: {self.cpu_limit.error}")
        else:
            self.on_event("cpu", f"CPU 上限 -> {percent}%（Windows 实际生效 "
                                 f"{self.cpu_limit_actual}%）")
        for e in self.engines:
            if e.proc and not e.set_cpu_percent(percent):
                self.on_event("cpu", f":{e.port} CPU 限速失败: {e.cpu_limit_error}")

    def core_pool_label(self) -> str:
        cpus = [int(c) for c in (self.cfg.get("engine_cores") or [])]
        try:
            limit = max(10, min(100, int(self.cfg.get("cpu_limit_percent") or 100)))
        except Exception:
            limit = 100
        actual = self.cpu_limit_actual
        cap = f"{limit}%" if actual is None else f"{limit}%（实际 {actual}%）"
        plan = self._log_cpu()
        if not cpus:
            if plan is not None:
                return (f"计算池 {len(plan['compute_ids'])} 个逻辑处理器"
                        f"（第 {plan['logical']} 号留给客户端），CPU上限 {cap}")
            total = len(cores.cpu_sets()) or len(cores.topology())
            return f"全部 {total} 核心，CPU上限 {cap}"
        if plan is not None and int(plan["logical"]) in cpus:
            cpus = [c for c in cpus if c != int(plan["logical"])]
            note = f"（第 {plan['logical']} 号留给客户端）"
        else:
            note = ""
        return (f"{len(cpus)} 核心: " + ",".join(str(c) for c in cpus)
                + note + f"  CPU上限 {cap}")

    # ------------------------------------------------------------- the books
    def points_now(self) -> tuple[int, int]:
        """(pending, confirmed) across this fleet, in points."""
        pending = self.task_status.pending_points_of(v.vid for v in self.verifiers)
        return pending, sum(v.confirmed_points for v in self.verifiers)

    def session_points_now(self) -> tuple[int, int]:
        """(pending, confirmed) earned BY THIS RUN.

        The account device list reports a device's lifetime total; the numbers a
        reader wants on screen are what this session added on top of the
        baseline captured on the first refresh.
        """
        return (self.task_status.pending_points_of(v.vid for v in self.verifiers),
                sum(v.session_confirmed for v in self.verifiers))

    @staticmethod
    def _rate(t0: float, p0: int, t1: float, p1: int) -> float | None:
        dt = (t1 - t0) / 60.0
        return ((p1 - p0) / dt) if dt > 0.05 else None

    def points_per_minute(self, window_s: float = 300.0) -> float | None:
        """CONFIRMED points per minute over the recent past. Confirmed only —
        pending points are a hope, and a rate built on them would be a lie."""
        h = list(self.points_history)
        if len(h) < 2:
            return None
        t1, p1 = h[-1]
        older = [x for x in h if t1 - x[0] <= window_s]
        t0, p0 = older[0]
        return self._rate(t0, p0, t1, p1)

    def points_per_minute_all(self) -> float | None:
        """CONFIRMED points per minute from the first sample to now.

        The first sample is taken the moment this client logs in, so this is the
        "since this session started" average — the number a reader can divide
        into the total to see how the evening is going.  It still only counts
        confirmed points; a task whose answers are still being checked does not
        move either figure."""
        h = list(self.points_history)
        if len(h) < 2:
            return None
        return self._rate(h[0][0], h[0][1], h[-1][0], h[-1][1])

    def _should_read_account(self) -> bool:
        return bool(self.remote.cookie
                    and time.time() - self.account_at > 25)

    async def _read_account_devices(self) -> dict | None:
        try:
            acc = await self._io(self.remote.account_devices)
        except Exception:
            return None
        if isinstance(acc, dict) and acc.get("ok"):
            return acc
        return None

    @staticmethod
    def _device_points_by_prefix(acc: dict) -> dict[str, int]:
        by_prefix: dict[str, int] = {}
        for d in acc.get("devices") or []:
            did = str(d.get("id") or "")
            if did:
                by_prefix[did] = int(d.get("points") or 0)
        return by_prefix

    @staticmethod
    def _device_points_for(vid: str, by_prefix: dict[str, int]) -> int | None:
        for did, pts in by_prefix.items():
            if vid.startswith(did):
                return pts
        return None

    def _store_claimed(self, claimed_now: list[str]) -> None:
        current = list(self.cfg.get("claimed") or [])
        if current == claimed_now:
            return
        self.cfg["claimed"] = claimed_now
        try:
            self.on_config_changed()
        except Exception:
            pass

    async def _claim_ready_verifiers(self) -> None:
        for v in self.verifiers:
            if (not v.account_claimed and v.lease is not None
                    and v.queued is not None and not v.claim_pending):
                await self._maybe_claim(v)

    async def _apply_device_points(self, acc: dict) -> None:
        self.account_points = int(acc.get("points") or 0)
        by_prefix = self._device_points_by_prefix(acc)
        claimed_now: list[str] = []
        for v in self.verifiers:
            hit = self._device_points_for(v.vid, by_prefix)
            was_claimed = v.account_claimed
            # THE SERVER DEVICE LIST IS AUTHORITATIVE.  A stale local
            # `claimed` marker used to suppress _maybe_claim forever,
            # leaving an unclaimed device's points uncredited.
            v.account_claimed = hit is not None
            if hit is not None:
                v.confirmed_points = hit
                v.points_known = True
                if not was_claimed:
                    # First authoritative reading after (re)claim: the run's
                    # baseline starts here, not before the device was ours.
                    v.base_confirmed = hit
                claimed_now.append(v.vid)
            else:
                v.confirmed_points = 0
                v.points_known = False
        self._store_claimed(claimed_now)
        await self._claim_ready_verifiers()
        self.account_at = time.time()

    async def _fill_unclaimed_points(self) -> None:
        """Unclaimed devices are the one case the per-device endpoint still
        answers with their own points. Claimed ones were filled above."""
        for v in self.verifiers:
            if v.confirmed_points:
                continue
            if (self._board_sync_active() and time.time() - float(
                    self._sync_points_at.get(v.vid, 0.0)) < 120.0):
                continue               # a recent sync already covered it
            try:
                r = await self._io(self.remote.points, v.vid)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("ok") and not r.get("claimed"):
                v.confirmed_points = int(r.get("points") or 0)
                v.points_known = True

    def _capture_point_baselines(self) -> None:
        """The first reading of each device is this run's baseline."""
        for v in self.verifiers:
            if v.points_known and v.base_confirmed is None:
                v.base_confirmed = v.confirmed_points

    def _record_points_history(self) -> None:
        self.points_at = time.time()
        self.points_history.append(
            (self.points_at, self.session_points_now()[1]))

    async def _refresh_points(self) -> None:
        """Read the account's authoritative per-device points.

        `/api/board/points` only reports points a device has earned before an
        account claimed it; once claimed it deliberately returns 0.  The
        account device list is the only endpoint that carries the real points
        for claimed devices, so that is the source used here.  Its ids are the
        first six characters of the verifier id, which is enough to map each
        list row back to this fleet.
        """
        await self._account_probe()
        if self._should_read_account():
            acc = await self._read_account_devices()
            if acc is not None:
                await self._apply_device_points(acc)
        await self._fill_unclaimed_points()
        self._capture_point_baselines()
        self._record_points_history()

    async def points_loop(self, every: float = 30.0) -> None:
        while self.running:
            try:
                await self._refresh_points()
            except Exception as e:
                self.on_event("error", f"points: {type(e).__name__}: {e}")
            await asyncio.sleep(every)

    async def task_status_loop(self, every: float = 30.0) -> None:
        """Refresh only open board/riven task states, once per upstream pace."""
        while self.running:
            try:
                await self._poll_task_statuses()
            except Exception as e:
                self.on_event("error", f"task status: {type(e).__name__}: {e}")
            await asyncio.sleep(every)

    # ------------------------------------------------- protocol negotiation
    def _persist_task_copy(self) -> None:
        """Optional extra copy of the in-memory task answers (opt-in).

        Memory remains authoritative: this never feeds a later run back.  It
        only drops a JSON snapshot when ``task_persist`` is enabled, so a
        crashed process can be inspected after the fact.  Off by default.
        """
        if not bool(self.cfg.get("task_persist")):
            return
        try:
            import json as _json
            path = paths.ROOT / "data" / "task_answers.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "riven_pending": [dict(x) for x in self._pending_riven_entries()],
                "board_kept": [
                    {"key": [str(k[0]), str(k[1])], "entry": v}
                    for k, v in getattr(self, "_board_kept", {}).items()
                ],
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2,
                                      default=str),
                           encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass

    def _board_sync_active(self) -> bool:
        """May this client use the board wire?  There is only one: protocol 8.

        EVERYTHING the client says about board work goes in one sync body —
        `held[]` renews, `results` hands in, `want_ms` asks, `states` asks about
        results.  The classic protocol-7 doors (`/work`, `/answers`, `/renew`,
        `/release`) were removed rather than kept as a second path: they were
        the second writer that raced the sync, and this client only ever sees a
        protocol-8 server.  The one way this returns False is the server's own
        `fallback: 7`, which is honoured by RESTING — never by speaking 7.
        """
        return not self._proto8_off

    @staticmethod
    def _sync_verifier_healthy_static(fails: int, backoff_until: float,
                                       now: float) -> bool:
        """A verifier's protocol-8 sync is healthy unless it is FAILING.

        Healthy = no failed sync in a row and not inside a transport backoff.
        This is the health signal the protocol-7 fallback path uses: with sync
        failing there is no renewal and no `fallback: 7` can arrive (that needs
        a successful sync), so the 7 loops must stay live in that window.

        NOT the pacing clock.  `_sync_next` is `next_ms` — minutes on an idle
        pool — and treating "not due yet" as "broken" made every verifier
        unhealthy for most of every cycle, which is what kept polling `/mine`,
        re-sending answers through `/answers` and renewing rows through
        `/renew` while protocol 8 was in fact working perfectly.
        """
        if int(fails or 0) > 0:
            return False
        return now >= float(backoff_until or 0.0)

    def _sync_verifier_healthy(self, v: Verifier) -> bool:
        return self._sync_verifier_healthy_static(
            self._sync_fails.get(v.vid, 0),
            self._sync_backoff_until.get(v.vid, 0.0), time.time())

    def _learn_proto_served(self, *sources) -> None:
        got = board_sync.offered_protocols(*sources)
        if not got:
            return
        merged = sorted(set(self._proto_served) | set(got))
        if merged != self._proto_served:
            self._proto_served = merged
            self.log.event("board_proto_served", served=self._proto_served)

    def _sync_source(self, work) -> bool:
        return (getattr(work, "source", "") == "sync"
                and self._board_sync_active())

    # ------------------------------------------------- protocol-8 sync body
    def _sync_held_rows(self, v: Verifier) -> list:
        """EVERY board row held and unanswered, grouped by lease.

        Naming a row renews it; a live lease this sync does not name is given
        back — so this is built from the works themselves, complete by
        construction, whatever door took them.
        """
        by_lease: dict[str, list] = {}
        for work, _role in v.works():
            if work is None or work.aborted or work.kind != "board":
                continue
            rows = [t.question for t in work.tasks
                    if t.question and not t.lost
                    and self._sync_row_named(work.lease_id, t)]
            if rows:
                bucket = by_lease.setdefault(work.lease_id, [])
                bucket.extend(q for q in rows if q not in bucket)
        return [{"lease": lease, "rows": rows}
                for lease, rows in by_lease.items()]

    def _sync_row_named(self, lease: str, task) -> bool:
        """Must this row be named in `held[]` right now?

        YES FOR ANY ROW WHOSE ANSWER IS STILL OWED (`_board_kept`), even one
        that has already finished.  The door has one rule — an unnamed live
        row is taken back — and it does not care that the answer is sitting in
        `results`: a completed row that stops being named has its lease
        released, and the sync that follows can only answer
        `dropped: {why: "not_held"}` for it.  Measured: exactly that lost one
        of four finished rows during a pause.  So a done row keeps its lease
        until `taken`/`dropped` says otherwise; `_sync_results_for` and this
        together are what make "finished" different from "credited".
        """
        if task.state in ("pending", "inflight"):
            return True
        return (lease, task.question) in self._board_kept

    def _sync_results_with_anchor(self, v: Verifier, held: list) -> list:
        """What this sync hands in: everything owed EXCEPT the anchor row.

        THE PACK IS NOT HELD FOR RANSOM, ONE ROW IS.  Waiting for a queued pack
        before handing anything in cost whole packs: a row another independent
        client answers first is settled out from under us — `dropped: {why:
        "not_held"}`, then `held: false` — and the doc is explicit that this is
        the first answer settling it, not a lapsed lease ("NOBODY IS CUT OFF.
        Past `ROW_CEILING_MS` … and the first answer settles it").  The one
        measured A/B we have is stark: the same account, the same five ids and
        the same lane plan, handing in ~3 s after a row finished, took 4776
        rows with ZERO lost in 2.26 h; holding answers for the pacing tick took
        0 and lost 61 in 11.5 min.

        SO: rows go in as they finish (`board_hand_in_rows` at a time, and the
        pack's tail at once), and exactly ONE row is kept back.  Holding one
        keeps the id from ever holding nothing — the server hands a riven only
        to an id holding nothing, which board-only then refuses — and it caps
        the damage at ONE row.

        THE ANCHOR IS HELD FOR `board_submit_hold_ms` — one hour — and it ends
        early for exactly one reason: THE ANCHOR WAS TAKEN FROM US.  A row
        another independent client answers first is settled out from under us
        (`held: false`, `dropped: {why: "not_held"}`) and leaves `_board_kept`;
        with nothing left to renew there is no anchor any more, so the pack goes
        and the id can be handed the next one.  That is what stops the wait from
        wedging: a pack whose anchor is gone cannot stay open for the hour.
        """
        owed = self._sync_results_for(v.vid)
        if not owed:
            self._anchor.pop(v.vid, None)
            return owed
        if v.queued is not None or not self._board_only(v):
            # The queue is in hand (or this is not board-only): the anchor has
            # done its job, so everything goes, anchor included — that is what
            # lets the pack close and the queued pack take over.
            self._anchor.pop(v.vid, None)
            return owed
        if not self._will_ask_again(v):
            # A RETIRING (or off-duty) ID HAS NOTHING TO WAIT FOR.  It will
            # never be handed another pack, so the anchor cannot buy it
            # anything — it only keeps this pack open for the full hour and
            # risks the one row it holds.  Hand it ALL in, close the pack, and
            # let the id go idle.
            self._anchor.pop(v.vid, None)
            self.log.event("board_anchor_skipped", verifier=v.vid,
                           rows=len(owed),
                           reason=("retiring" if getattr(v, "retire_at", 0)
                                   else "off_duty"))
            return owed
        lease = str(owed[0].get("lease") or "")
        st = self._anchor.get(v.vid)
        if st is None or st.get("lease") != lease:
            # A NEW PACK: anchor on the NEWEST answer.  Newest, not oldest —
            # it is the newest that has been exposed to the other holder for
            # the shortest time, so it is the one most likely to still be ours
            # when the queued pack arrives.
            st = {"lease": lease, "question": str(owed[-1].get("question") or ""),
                  "released": False, "at": time.time()}
            self._anchor[v.vid] = st
        if st.get("released"):
            return owed          # this pack gave its anchor up; let it close
        anchor_q = str(st.get("question") or "")
        if not any(str(a.get("question")) == anchor_q for a in owed):
            # TAKEN FROM US.  `held: false` / `dropped: {why: "not_held"}` pulls
            # the row out of `_board_kept`, so there is nothing left to renew
            # and nothing left to hold: anchoring on another row in its place
            # would only be taken the same way, and would keep this pack open
            # for the full hour.  So the pack goes, and the id can be handed the
            # next one.
            st["released"] = True
            self.log.event("board_anchor_lost", verifier=v.vid, lease=lease,
                           question=anchor_q, rows=len(owed), queued=False)
            return owed
        hold = self._submit_hold_s()
        age = self._answer_age_s(v, {"lease": lease, "question": anchor_q})
        if age >= hold:
            st["released"] = True
            self.log.event("board_anchor_released", verifier=v.vid, lease=lease,
                           question=anchor_q, rows=len(owed),
                           anchor_age_s=round(age, 1), hold_s=hold, queued=False)
            return owed
        # ONE ROW STAYS.  With a single row owed that means an empty `results`:
        # that row IS the anchor, and handing it in would leave the id holding
        # nothing, which is the state the anchor exists to avoid.
        self.log.event("board_submit_held", verifier=v.vid, via="sync",
                       lease=lease, question=anchor_q, anchor=1,
                       rows=len(owed) - 1, level="anchor", queued=False,
                       anchor_age_s=round(age, 1), hold_s=hold,
                       hold_left_s=round(max(0.0, hold - age), 1))
        return [a for a in owed if str(a.get("question")) != anchor_q]

    def _answer_age_s(self, v: Verifier, answer: dict) -> float:
        """How long THIS answer has been waiting — its own clock, not the pack's.

        The anchor is released on its own age: the row that is being kept back
        is the one whose risk is being taken, so the pack's oldest row (long
        since handed in) must not shorten or extend its budget.
        """
        lease = str(answer.get("lease") or "")
        q = str(answer.get("question") or "")
        entry = self._board_kept.get((lease, q)) or {}
        kept_at = float(entry.get("kept_at") or 0.0)
        return max(0.0, time.time() - kept_at) if kept_at else 0.0

    def _mark_sync_settled(self, v: Verifier, questions: list) -> int:
        """THE SERVER HAS SETTLED THESE ROWS: stop calling them owed.

        Protocol 8 never set `uploaded` on the way out — the answer rides the
        sync body — so `_board_unsent()` kept listing rows the server had
        already recorded, and the client went on offering them on the
        `/answers` door as well.  `taken` / `dropped` / `states: gone` is
        exactly where a row stops being owed.

        The counters are NOT touched here: `_credit_sync_taken` and
        `mark_submitted` already account for the answer, and counting it twice
        is how a "本次已确认" figure starts lying.
        """
        wanted = {str(q) for q in questions if q}
        if not wanted:
            return 0
        settled = 0
        for work, _role in v.works():
            if work is None or work.kind != "board":
                continue
            for t in work.tasks:
                if t.question in wanted and not t.uploaded:
                    t.uploaded = True
                    settled += 1
        return settled

    def _mark_answers_sent(self, results: list) -> int:
        """Remember which answers have ACTUALLY left this machine.

        The difference matters because of one word: `states` asks the server
        "where does this result stand", and the server answers from
        `worker/pg/sync.sql` `factory_states`, whose `mine` column is *this
        device's non-void answers for that question*; `worker/sync.js`
        `stateOf` returns `"gone"` the moment `mine` is 0, before it looks at
        anything else.  So an answer we are holding back is answered `gone`
        every single time — "I have nothing from you" — and reading that as
        "the row is lost" deleted the held answer and booked it 无效.

        Both `board_anchor_lost` events on 2026-10-16 03:07 were exactly this:
        the same reply carried `held: true` (the lease had not moved at all)
        and `states: gone`.  Nothing was stolen; the client threw its own
        withheld answer away.
        """
        sent = 0
        for a in results or []:
            if not isinstance(a, dict):
                continue
            key = (str(a.get("lease") or ""), str(a.get("question") or ""))
            entry = self._board_kept.get(key)
            if entry is not None and not entry.get("sent"):
                entry["sent"] = True
                sent += 1
        return sent

    def _sync_answer_sent(self, vid: str, question: str) -> bool:
        """Has this answer ever gone out in a sync's `results`?"""
        for (_lease, q), e in self._board_kept.items():
            if q == question and e.get("vid") == vid:
                return bool(e.get("sent"))
        return False

    def _touch_sync_held(self, v: Verifier, held: list, reply: dict) -> None:
        """A SUCCESSFUL SYNC RENEWED EVERY ROW IT NAMED — move the window.

        The server answers each named row with
        `lease_until = max(lease_until, now + sync_lease_ms)` (25 minutes),
        but nothing here did the same locally, so `left_s` kept counting down
        the ORIGINAL pack lease (five minutes), hit zero, and ran negative
        while the lease itself sat comfortably alive.  Push every named row's
        `row_until` up to the renewed span (minus the usual safety margin).
        Rows the reply took back — `dropped`, `held:false`, `withdrawn` — are
        left alone; they are being stopped anyway.
        """
        gone = {str((d or {}).get("question"))
                for d in reply.get("dropped") or []
                if isinstance(d, dict) and d.get("question")}
        gone |= {str(i.get("question")) for i in reply.get("held") or []
                 if isinstance(i, dict) and i.get("held") is False}
        gone |= {str(q) for q in reply.get("withdrawn") or [] if q}
        try:
            ms = int(self._sync_dials.get("sync_lease_ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        if ms <= 0:
            ms = 1_500_000            # dial default: twenty-five minutes
        until = time.time() + ms / 1000.0 - BOARD_LEASE_SAFE_S
        by_lease = {}
        for group in held or []:
            if isinstance(group, dict) and group.get("lease"):
                by_lease[str(group["lease"])] = group.get("rows") or []
        if not by_lease:
            return
        for work, _role in v.works():
            if work is None or work.aborted or work.kind != "board":
                continue
            rows = by_lease.get(str(work.lease_id))
            if not rows:
                continue
            for t in work.tasks:
                if (t.question in rows and not t.lost
                        and t.question not in gone):
                    t.row_until = max(t.row_until, until)

    def _sync_results_for(self, vid: str) -> list:
        """Kept answers for this id, oldest first — they ARE the `results`."""
        items = sorted(
            ((k, e) for k, e in self._board_kept.items()
             if e.get("vid") == vid),
            key=lambda kv: float(kv[1].get("kept_at") or 0.0))
        out = []
        for (lease, _question), entry in items:
            answer = dict(entry.get("answer") or {})
            if not answer:
                continue
            answer["lease"] = lease
            out.append(answer)
        return out[:board_sync.SYNC_RESULTS_MAX]

    def _sync_pending_board_rows(self, v: Verifier) -> int:
        return sum(1 for work, _ in v.works()
                   if work is not None and not work.aborted
                   and work.kind == "board"
                   for t in work.tasks
                   if not t.lost and t.state in ("pending", "inflight"))

    def _sync_kept_for(self, vid: str) -> list:
        return [(k, e) for k, e in self._board_kept.items()
                if e.get("vid") == vid]

    def _drain_pace_ms(self) -> int:
        """Wall-clock milliseconds per board row as THIS client drains them.

        `pace_ms` is one row on ONE engine — that is what the server wants, to
        size the pack it hands out.  The refill gate is a different question:
        how long until the pack in hand is gone, and that is `pace` divided by
        the slots eating it in parallel.  Feeding the raw number in made the
        gate open about a slot-count too late: measured with pace 8 991 ms and
        three slots, `pending * pace < sync_low_ms (120 s)` only became true at
        13 of 35 rows — after the last sync beat — so the ahead pack arrived
        after the pack in hand had already finished and the verifier sat empty
        for one more beat."
        """
        try:
            slots = max(1, int(self.slot_count()))
        except Exception:
            slots = 1
        try:
            pace = max(100, int(self.pace_ms))
        except (TypeError, ValueError):
            pace = 1000
        return max(100, pace // slots)

    async def _read_work_signal(self) -> dict | None:
        """Cached work signal; throttled to `poll_ms`, None when unusable."""
        now = time.time()
        # Negative cache on a failed/transport-error signal so a down id does
        # not get polled on every verifier of every round.
        if now < getattr(self, "_signal_failed_until", 0.0):
            return None
        if (self._signal_cache is not None
                and now - self._signal_at
                < self._sync_dials.get("poll_ms") / 1000.0):
            return self._signal_cache
        try:
            body = await self._io(board_sync.fetch_work_signal)
        except Exception:
            self._signal_failed_until = now + 300.0
            return None
        if not isinstance(body, dict):
            self._signal_failed_until = now + 120.0
            return None
        self._signal_cache = body
        self._signal_at = now
        self._signal_failed_until = 0.0
        self._learn_proto_served(body)
        return body

    async def _sync_idle_due(self, v: Verifier, now: float,
                         want_full: int) -> tuple:
        """An id holding nothing syncs only on the work signal's word."""
        dials = self._sync_dials
        if now < self._signal_wait_until.get(v.vid, 0.0):
            return False, "", 0
        body = await self._read_work_signal()
        if board_sync.signal_fallback(body, now):
            wait = 120.0 + random.uniform(
                0, dials.get("jitter_ms") / 1000.0)
            self._signal_wait_until[v.vid] = now + wait
            return True, "signal-fallback", want_full
        assert isinstance(body, dict)
        if body.get("engine") and body.get("engine") != self.served_engine:
            self.on_event("update", f"work signal 在线引擎 {body.get('engine')} "
                                    f"≠ 本地 {self.served_engine or '?'}，"
                                    f"更新检查会跟进")
        lo, hi = board_sync.signal_wait_bounds(body, dials)
        if lo < 0:
            self._signal_wait_until[v.vid] = (
                now + dials.get("poll_ms") / 1000.0)
            return False, "", 0
        wait = board_sync.roll_wait((lo, hi)) or 0.0
        self._signal_wait_until[v.vid] = (
            now + max(wait, 1.0) + dials.get("poll_ms") / 1000.0)
        return True, "signal", want_full

    async def _sync_due(self, v: Verifier,
                        now: float) -> tuple[bool, str, int]:
        """(due, reason, want_ms).  Never raises; network only for idle ids."""
        if not self._board_sync_active() or v.vid is None:
            return False, "", 0
        if now < float(getattr(v, "banned_until", 0.0) or 0.0):
            # Keep the normal pacing so a banned verifier does not hammer the
            # sync doors; treat the ban window as the next allowed time.
            self._sync_next[v.vid] = max(
                float(self._sync_next.get(v.vid, 0.0) or 0.0),
                float(getattr(v, "banned_until", 0.0) or 0.0))
            return False, "", 0
        dials = self._sync_dials
        paused = bool(self.paused or self.maintenance)
        pending = self._sync_pending_board_rows(v)
        kept = self._sync_kept_for(v.vid)
        refused = self._sync_refused.get(v.vid)
        want = 0 if paused else self._want_ms_for(v, pending, dials, now)
        if self._sync_may_rest(v, want, kept, refused, now):
            # Asking for nothing, nothing owed, and a riven on an engine: a
            # sync now would only hand the riven's lease back (see the helper)
            # — or, in board-only cooling, would just be sent back `early`.
            return False, "", 0
        last = float(self._sync_last.get(v.vid) or 0.0)
        nxt = float(self._sync_next.get(v.vid) or 0.0)
        if last <= 0 and nxt <= 0:
            # Never synced: ask at once (or report if there is no room).
            if want > 0:
                return True, "first", want
        # HAND IN AS THE PACK FILLS; KEEP ONE ROW BACK.
        #
        # `board_hand_in_rows` finished rows are enough to justify a sync, and
        # the pack's TAIL (nothing left to compute, rows still owed) goes at
        # once.  Both are served whether or not the server calls the sync
        # `early`, which is why they do not wait for the tick; the ask still
        # does, so they carry `want_ms: 0` until the tick comes round.
        #
        # The tick sync below is what renews the one row that IS held back —
        # the anchor (`_sync_results_with_anchor`) — along with every other row
        # still named, so waiting for a queued pack cannot lapse a lease.
        owed = len(kept)
        results_wait = 3.0 if self._board_only(v) else 30.0
        threshold = int(self.cfg.get("board_hand_in_rows") or 10)
        tail = owed > 1 and pending == 0
        if owed and now - last >= results_wait and (
                owed >= threshold or tail
                or nxt <= 0 or now >= nxt
                or now - last >= RESULTS_HOLD_MAX_S):
            early = nxt > 0 and now < nxt
            return True, "results", (0 if early else want)
        # `next_ms` is BOTH the work gate and the heartbeat tick: every sync
        # renews every named row, and asking early only costs an `early: true`
        # (results and renewals are still served).  So a holder syncs on the
        # tick, period — gating work-asks on "running low" here would let a
        # full pack lapse unrenewed between ticks.
        if pending > 0 and (nxt <= 0 or now >= nxt):
            return True, "heartbeat", want
        report = dials.get("report_ms")
        if report > 0 and now - last >= report / 1000.0:
            return True, "report", 0
        if not kept and pending <= 0:
            return await self._sync_idle_due(v, now, want)
        return False, "", 0

    def _verifier_on_duty(self, v: Verifier) -> bool:
        """Whether this device may ASK for work under protocol 8.

        Only the first `active_verifiers` ids work; anything beyond that is a
        spare, woken (`v.spare`) when the residents run short.  A retiring
        spare keeps syncing until what it holds is delivered — its rows would
        die unnamed otherwise — but `_want_ms_for` has already taken its ask
        away, so it never picks up new work.
        """
        if v.spare:
            if not v.retire_at:
                return True
            holds = any(w is not None and not w.aborted for w, _ in v.works())
            return (holds or bool(self._sync_kept_for(v.vid))
                    or bool(self._sync_refused.get(v.vid)))
        idx = next((i for i, x in enumerate(self.verifiers) if x is v), -1)
        return 0 <= idx < self.active_count()

    def _board_want_ms(self, dials) -> int:
        """How much work to ask for, in the server's own units.

        `want_ms` IS "how much work to be handed" (docs/CLIENTS.md §Values), and
        the pack is `round(want_ms / pace_ms)` rows.  The classic ask is
        `pack8_ms` itself — the biggest pack the server will hand — and its cost
        is now measured: on the 56-minute reference session (33 packs, 62.5 rows
        each) the MEDIAN row waited **463 s** from arriving to being computed,
        p90 983 s, worst 1216 s, and 93.9% waited over four minutes.  That wait
        is the window in which another client may settle the row first.

        HOLDING LESS IS A TRADE, NOT A FREE WIN.  A smaller pack shortens the
        row's exposure and the time a pack stays open (the anchor waits for the
        next one), but every pack costs a sync round trip — measured 0.7-3.5 s —
        and those run on the client's own CPU, which competes with the lanes for
        logical processors (the same contention the utilisation work is about).

        `0` — the default — keeps the classic ask.  Set `board_want_ms` to try a
        smaller one in the field.
        """
        want = int(self.cfg.get("board_want_ms") or 0)
        top = int(dials.get("pack8_ms") or 0)
        if want <= 0:
            return top or None
        return min(want, top) if top else want

    def _want_ms_for(self, v: Verifier, pending: int, dials,
                     now: float) -> int:
        """How much work to ask for, by mode.

        BOARD ONLY (config `queue_fallback=1`): ask for a pack whenever the queue
        slot is free, whatever is left in hand — the whole point of the mode is
        that the client never holds nothing, because the server hands a riven
        to a computer that holds nothing (worker/sync.js).  `packs_held` on the
        server side is the ceiling, so this cannot over-ask.

        BOTH (0): one work at a time, exactly as the official page runs — ask
        only when this verifier holds nothing, so work is never hoarded and a
        riven can reach it.  The pacing rule (`sync_low_ms`) is then only a
        floor.
        """
        if not self._verifier_on_duty(v):
            return 0                    # off duty (beyond active count / retired)
        if v.riven_cooldown_until and now < v.riven_cooldown_until:
            return 0                    # just gave a riven back; stay quiet
        if v.retire_at:
            return 0                    # a RETIRING spare never picks up new
                                        # work — in EITHER mode (board-only
                                        # would otherwise hand it a fresh ask
                                        # for a pack it must not start)
        if self._board_only(v):
            if v.queued is not None:
                return 0
            return self._board_want_ms(dials)
        if v.lease is not None or v.queued is not None:
            return 0
        return board_sync.compute_want_ms(
            pending, self._drain_pace_ms(), dials, True)

    def _sync_may_rest(self, v: Verifier, want: int, kept: list,
                       refused, now: float) -> bool:
        """True when this verifier is better off NOT syncing this pass.

        Two cases, both "a request now buys nothing and costs something":

        RIVEN REST — a riven is on an engine and there is nothing only a sync
        can deliver (`_riven_work_alone`): the request would name rows the
        riven is not among and hand its lease back mid-search.

        BOARD-ONLY COOLING — the verifier just gave a riven back and the
        server will not hand out anything before its own 60 s cooling anyway
        (`alone()` sets `next_at`), so the beat would come back `early` and
        empty.  Anything to deliver (`kept`/`refused`) still goes.
        """
        if want:
            return False                  # there is work to ask for: sync
        if kept or refused:
            return False                  # there is an answer to hand in: sync
        if now < float(v.riven_cooldown_until or 0.0):
            return True                   # board-only cooling
        return self._riven_work_alone(v, kept, refused)

    def _spare_evaluate(self, now: float) -> None:
        """Keep one spare verifier awake whenever the residents run short.

        The goal: as many WORKING verifiers as there are compute slots.  A
        verifier counts while it holds a pack or a riven; one sitting out its
        riven cooldown does not (it cannot ask for 70 s).  The residents
        covering every slot for `SPARE_RETIRE_STABLE_S` straight stands the
        spare down — no time cap the other way: a spare works as long as the
        residents stay short.  A retirement already under way is cancelled the
        moment the residents fall short again, so the two states cannot flap.
        """
        if not self.running or self.paused or self.maintenance:
            return
        slots = self.slot_count()
        residents = self.verifiers[:self.active_count()]
        spares = self.verifiers[self.active_count():]
        if not spares:
            return

        def has_work(v: Verifier) -> bool:
            if v.riven_cooldown_until > now:
                return False              # cooling: cannot ask for 70 s
            if v.lease is not None or v.queued is not None:
                return True
            task = self.fetch_tasks.get(v.vid)
            return bool(task and not task.done())

        residents_working = sum(1 for v in residents if has_work(v))
        working = residents_working + sum(
            1 for v in spares if v.spare and has_work(v))

        if residents_working >= slots:
            # COVERED: after a stable stretch, stand the spares down.
            self._spare_covered_since = self._spare_covered_since or now
            if now - self._spare_covered_since >= SPARE_RETIRE_STABLE_S:
                for v in spares:
                    if v.spare and not v.retire_at:
                        v.retire_at = now
                        self.log.event("spare_retiring", verifier=v.vid)
                        self.on_event("update",
                                      f"{v.label} 常驻已覆盖全部计算进程，"
                                      f"备胎算完手头任务后退役")
            return
        self._spare_covered_since = 0.0
        # SHORT: cancel any retirement under way, then wake what is missing.
        for v in spares:
            if v.spare and v.retire_at:
                v.retire_at = 0.0
                self.log.event("spare_retire_cancelled", verifier=v.vid)

        # THE STAIRCASE.  A resident with no pack THIS INSTANT is between two
        # packs, not short of work: waking a spare for that is how three spares
        # came up together on 2026-10-16 07:13 (`residents_working: 0` for a
        # second, `spare_activated` ×3, all three stood down 34 s later) and paid
        # three asks for nothing.
        #
        # So a spare is woken only when the pool is PROVABLY out of work —
        # EVERY resident sitting in its riven cool-down (`riven_cooldown_until`,
        # the server's own fixed 70 s ban after a riven came back) — and then
        # EXACTLY ONE goes up.  The next one waits until that spare is in
        # cool-down too, so the spares arrive one at a time, each on the
        # server's own signal, and never more than the shortfall needs.
        #
        # NOTHING IS HARD-CODED TO THREE (or any number): `residents` is
        # `active_count()` worth of verifiers and `spares` is the rest, so a
        # config with four residents and two spares, or one resident and nine,
        # takes the same path.
        def cooling(v: Verifier) -> bool:
            return v.riven_cooldown_until > now

        active_spares = [v for v in spares if v.spare]
        residents_cooling = sum(1 for v in residents if cooling(v))
        spares_cooling = sum(1 for v in active_spares if cooling(v))
        residents_all_cooling = bool(residents) and residents_cooling == len(residents)
        spares_all_cooling = spares_cooling == len(active_spares)
        if not (residents_all_cooling and spares_all_cooling):
            # OBSERVABLE, BUT NOT PER BEAT: this runs every 0.25 s, and a line
            # per beat would be 4/s of log writing — the very CPU the lanes are
            # fighting for (see the 2026-10-16 utilisation work).
            if now - getattr(self, "_spare_hold_at", 0.0) >= 10.0:
                self._spare_hold_at = now
                self.log.event("spare_hold", residents=len(residents),
                               residents_cooling=residents_cooling,
                               spares_active=len(active_spares),
                               spares_cooling=spares_cooling,
                               slots=slots, working=working,
                               reason="cooldown_gate")
            return
        for v in spares:
            if v.spare:
                continue
            v.spare = True
            self.log.event("spare_activated", verifier=v.vid,
                           residents=len(residents),
                           residents_cooling=residents_cooling,
                           spares_active=len(active_spares) + 1,
                           slots=slots, working=working,
                           reason="residents_all_cooling")
            self.on_event("update",
                          f"{v.label} 备胎上线：常驻 {len(residents)} 个已全部进入"
                          f"冷却，阶梯启动一个")
            break

    def _riven_work_alone(self, v: Verifier, kept: list, refused) -> bool:
        """True while a riven search is running on this verifier.

        The sync is the riven's enemy: `board_sync` runs
        `factory_release_unnamed`, which clears EVERY question leased to this
        device that the request does not name — and a riven's lease lives on a
        question row (`worker/appraise.js`) that a sync can never name, so any
        sync fired while the search runs takes the riven back mid-compute.

        Resting protects BOTH sides: no request, no release, so pack rows held
        beside the riven keep their leases too (a short pack gets
        `pack_lease_ms`, five minutes; the next sync that does go out renews
        whatever remains to `sync_lease_ms`, twenty-five).  A spare never
        reaches this state any more — board-only makes it hand a riven back at
        once — but the guard stays correct for mode 0.

        The beat still goes out when there is something only a sync can do:
        answers owed (`kept`) or a refusal to report.  Rows merely computing
        are NOT such a thing — that is the whole point.
        """
        if kept or refused:
            return False
        if self._board_only(v):
            # A board-only verifier never KEEPS a riven — one arriving is given
            # back at once — so there is nothing to protect by resting, and the
            # queue heartbeat must not be skipped on a stray arrival's account.
            return False
        return any(w.kind == "riven_gain" and not w.aborted
                   and (w.inflight_tasks() or w.pending_tasks())
                   for w, _ in v.works())

    async def _sync_verifier(self, v: Verifier, reason: str,
                             want: int) -> None:
        """One sync: name everything, hand in results, obey the reply."""
        now = time.time()
        lanes = int(self.cfg.get("work_lanes") or 16)
        pace = board_sync.clamp_pace_ms(self.pace_ms, self._sync_dials)
        held = self._sync_held_rows(v)
        # THE HAND-IN GATE, ON THE DOOR THE ANSWERS ACTUALLY USE.  Protocol 8
        # carries them in this body, so `_sync_results_with_anchor` here IS the
        # policy: everything owed except the one anchor row.  The anchored row
        # stays named below, so its lease keeps being renewed while it waits.
        results = self._sync_results_with_anchor(v, held)
        self._mark_answers_sent(results)
        refused = self._sync_refused.pop(v.vid, [])
        idle = board_sync.idle_payload(self._sync_idle, v.vid)
        # `states` ASKS ABOUT RESULTS THE SERVER HAS.  It is answered from
        # `factory_states`' `mine` — that device's non-void answers — and
        # `stateOf` returns `gone` when it is 0.  So asking about an answer we
        # are deliberately HOLDING (the anchor) comes back `gone` every time,
        # and treating that as "the row is lost" threw the held answer away and
        # counted it 无效: both `board_anchor_lost` events were that, not a
        # theft — the same reply said `held: true`.  Only ask about what has
        # really left this machine.  `≤ 100` is the wire's own cap.
        states = [q for (_l, q), e in self._sync_kept_for(v.vid) if e.get("sent")]
        states = states[:SYNC_STATES_MAX]
        body = board_sync.build_sync_body(
            verifier=v.vid, engine=self.engine_id or "",
            consent_v=CONSENT_V, consent_at=utc_iso(),
            lanes=lanes, pace_ms=pace, want_ms=want, held=held,
            results=results, refused=refused or None,
            idle=idle or None, states=states or None,
            since=self._points_since or self._local_midnight_utc_hour())
        try:
            out = await self._io(self.remote.board_sync, body)
        except Exception as e:
            self._sync_refused[v.vid] = refused + self._sync_refused.get(
                v.vid, [])
            self._sync_fails[v.vid] = int(self._sync_fails.get(v.vid) or 0) + 1
            # THE PAGE'S OWN LADDER (69-board-work.js `SEND_AGAIN_MS`): 3, 10,
            # 30 seconds, then 30.  The old 30·2^n ladder topped out at five
            # minutes, and a short pack's lease is `pack_lease_ms` = five
            # minutes: one slow backoff and the rows lapse before the next
            # sync.  A short ladder costs a few extra round trips; a long one
            # costs the pack.  This only sets when the verifier is due again —
            # nothing blocks, so a fast retry cannot stall the poll loop.
            backoff = board_sync.sync_backoff_s(self._sync_fails.get(v.vid) or 0)
            self._sync_next[v.vid] = now + backoff
            self._sync_backoff_until[v.vid] = now + backoff
            board_sync.idle_tick(self._sync_idle, v.vid,
                                 board_sync.utc_hour(now), asks=1, failed=1)
            self.log.event("sync_transport_error", verifier=v.vid,
                           reason=reason,
                           error=f"{type(e).__name__}: {e}")
            return
        if not isinstance(out, dict) or not out.get("ok"):
            self._sync_refused[v.vid] = refused + self._sync_refused.get(
                v.vid, [])
            board_sync.idle_tick(self._sync_idle, v.vid,
                                 board_sync.utc_hour(now), asks=1, failed=1)
            self.log.event("sync_error", verifier=v.vid, reason=reason,
                           response=out)
            return
        self._sync_fails[v.vid] = 0
        self._sync_backoff_until[v.vid] = 0.0
        self._sync_last[v.vid] = now
        board_sync.idle_tick(
            self._sync_idle, v.vid, board_sync.utc_hour(now), asks=1,
            lanes=0 if (out.get("pack") or out.get("work")) else lanes)
        if out.get("kept"):
            board_sync.idle_clear(self._sync_idle, v.vid)
        outcome = await self._apply_sync_reply(v, out, want)
        if outcome == "ok":
            # The reply just renewed every row this sync named: follow it, or
            # the UI's lease countdown runs off the original pack lease and
            # shows negative seconds on a perfectly alive lease.
            self._touch_sync_held(v, held, out)
        return outcome

    def _apply_sync_points(self, v: Verifier, points) -> bool:
        """Feed a sync reply's `points` body into the points pipeline.

        Claimed devices are authoritative from the account device list — a
        sync value never overwrites them.  An unclaimed device takes it
        exactly like the `/board/points` poll this replaces while sync covers
        it (same body shape, same baseline capture on the next points loop).
        """
        if not isinstance(points, dict) or points.get("ok") is False:
            return False
        if getattr(v, "account_claimed", False):
            self.log.event("board_sync_points_claimed", verifier=v.vid)
            return False
        if "points" not in points:
            return False
        try:
            v.confirmed_points = int(points.get("points") or 0)
        except (TypeError, ValueError):
            return False
        v.points_known = True
        self._sync_points_at[v.vid] = time.time()
        self.log.event("board_sync_points", verifier=v.vid,
                       points=v.confirmed_points)
        return True

    def _sync_confirm_orders(self, vid: str, confirmed: list,
                             gone: list) -> tuple[int, int]:
        """Feed sync `taken`/`dropped`/`states` into the ledger by order."""
        orders: list = []
        for question, state in ([(q, "confirmed") for q in confirmed]
                                + [(q, "gone") for q in gone]):
            triple = None
            for (_lease, q), entry in self._board_kept.items():
                if entry.get("vid") == vid and q == question:
                    triple = (entry.get("identity") or "",
                              entry.get("ruler") or "",
                              entry.get("mode") or "")
                    break
            if triple is None:
                for vv in self.verifiers:
                    if vv.vid != vid:
                        continue
                    for work, _ in vv.works():
                        if work is None or work.kind != "board":
                            continue
                        for t in work.tasks:
                            if t.question == question:
                                triple = self._board_order_of(t)
                                break
            if triple and any(triple):
                orders.append({"identity": triple[0], "ruler": triple[1],
                               "mode": triple[2], "state": state})
        if not orders:
            return 0, 0
        before = self.task_status.counts(vid)
        self.task_status.apply_board_mine(
            vid, {"ok": True, "orders": orders}, time.time())
        after = self.task_status.counts(vid)
        return (after.get("confirmed", 0) - before.get("confirmed", 0),
                after.get("gone", 0) - before.get("gone", 0))

    def _credit_sync_taken(self, v: Verifier, questions) -> int:
        """Count the rows the server just TOOK as uploaded.

        Under protocol 8 there is no `/answers` reply to hang the tally on:
        `taken` is the only thing that means "the server has it".  Counting
        nothing here is why the header read `上传 0 (榜单 7xx)` — the second
        number counts rows fought, the first rows credited, and only the
        protocol-7 path ever credited one.
        """
        wanted = {str(q) for q in (questions or []) if q}
        if not wanted:
            return 0
        landed = 0
        for work, _role in v.works():
            if work is None or work.kind != "board":
                continue
            batch = [t for t in work.tasks
                     if t.question in wanted and not t.uploaded
                     and t.result is not None]
            if batch:
                landed += v.mark_uploaded(work, batch)
        return landed

    def _drop_sync_answered(self, vid: str, questions) -> int:
        n = 0
        for question in questions:
            for key in [k for k, e in self._board_kept.items()
                        if e.get("vid") == vid and k[1] == question]:
                if self._board_kept.pop(key, None) is not None:
                    n += 1
        return n

    def _stop_sync_row(self, v: Verifier, question: str,
                       why: str = "held:false") -> bool:
        """A sync says this row is no longer ours: stop it now."""
        for work, _role in v.works():
            if work is None or work.kind != "board":
                continue
            for t in work.tasks:
                if t.question != question or t.lost:
                    continue
                t.lost = True
                self.task_status.mark_local_failure(v.vid, t, "sync_" + why)
                self._drop_board_kept(work.lease_id, question, "sync " + why)
                self.log.event("row_lost", verifier=v.vid,
                               lease=work.lease_id, question=question,
                               via="sync", why=why)
                owner = self._slot_for_work(v, work)
                if owner is not None:
                    self._deadline_restart(v, work, True)
                self.on_event("deadline",
                              f"{v.label} 一行已被收走，停算（sync {why}）")
                return True
        return False

    async def _apply_sync_reply(self, v: Verifier, reply: dict,
                                want: int) -> str:
        """Apply one sync reply; returns a short outcome for the log."""
        now = time.time()
        self._learn_proto_served(reply)
        self._sync_dials.merge(reply.get("dials"))
        if reply.get("release"):
            self.release = reply["release"]
        if reply.get("fallback"):
            # PROTOCOL 8 ONLY — THERE IS NO 7 TO SWITCH TO.  Honoured by
            # resting: the fleet stops asking and the console says why, rather
            # than speaking a wire this client no longer implements.
            if not self._proto8_off:
                self._proto8_off = True
                self.on_event("update",
                              "服务端已关闭协议8：本客户端只走协议8，暂停领取任务")
                self.log.event("board_proto8_refused",
                               fallback=reply.get("fallback"),
                               served=self._proto_served)
            return "fallback"
        if reply.get("stale"):
            self.on_event("engine_stale",
                          "sync 报告引擎或协议已过期，请等待更新检查跟进")
            self.log.event("board_sync_stale", verifier=v.vid)
            return "stale"
        if reply.get("banned"):
            try:
                v.banned_until = float(reply.get("until") or 0) / 1000.0
            except (TypeError, ValueError):
                v.banned_until = now + 300.0
            self.log.event("board_sync_banned", verifier=v.vid,
                           until=v.banned_until)
            return "banned"
        self._apply_sync_points(v, reply.get("points"))
        vid = v.vid
        taken = [str(q) for q in reply.get("taken") or [] if q]
        dropped = [str((d or {}).get("question"))
                   for d in reply.get("dropped") or []
                   if isinstance(d, dict) and d.get("question")]
        if taken or dropped:
            confirmed, gone = self._sync_confirm_orders(vid, taken, dropped)
            landed = self._credit_sync_taken(v, taken)
            self.stats["uploaded"] += landed
            self._mark_sync_settled(v, taken + dropped)
            erased = self._drop_sync_answered(vid, taken + dropped)
            self.log.event("board_sync_confirmed", verifier=v.vid,
                           taken=len(taken), dropped=len(dropped),
                           confirmed=confirmed, gone=gone, erased=erased,
                           uploaded=landed, kept=len(self._board_kept))
        for item in reply.get("states") or []:
            if not isinstance(item, dict) or not item.get("question"):
                continue
            state = str(item.get("state") or "")
            if state == "confirmed":
                landed = self._credit_sync_taken(v, [str(item["question"])])
                self.stats["uploaded"] += landed
                self._sync_confirm_orders(vid, [str(item["question"])], [])
                self._mark_sync_settled(v, [str(item["question"])])
                self._drop_sync_answered(vid, [str(item["question"])])
            elif state == "gone":
                q = str(item["question"])
                if not self._sync_answer_sent(vid, q):
                    # `gone` MEANS "THE SERVER HAS NO ANSWER FROM US" — `mine`
                    # is 0 in `factory_states`, and `stateOf` maps that to
                    # `gone` before it ever looks at the question's own state.
                    # For a row we are holding back that is exactly what we
                    # expect, so dropping it here would throw finished work
                    # away: the anchor's whole job is to hold one row, and this
                    # branch was destroying it every time (measured 2 for 2;
                    # the same reply said `held: true` — no lease moved).
                    self.log.event("sync_state_gone_unsent", verifier=v.vid,
                                   question=q)
                    continue
                self._sync_confirm_orders(vid, [], [q])
                self._mark_sync_settled(v, [q])
                self._drop_sync_answered(vid, [q])
        stopped = 0
        malformed = 0
        for item in reply.get("held") or []:
            if not isinstance(item, dict) or not item.get("question"):
                malformed += 1
                continue
            if item.get("held") is False:
                if self._stop_sync_row(v, str(item["question"])):
                    stopped += 1
        for question in reply.get("withdrawn") or []:
            if not question:
                malformed += 1
                continue
            if self._stop_sync_row(v, str(question), "withdrawn"):
                stopped += 1
        if malformed:
            self.log.event("board_sync_malformed", verifier=v.vid,
                           held_malformed=malformed)
        if stopped:
            self.log.event("board_sync_stopped", verifier=v.vid,
                           rows=stopped)
        pack = reply.get("pack")
        if isinstance(pack, dict) and pack.get("tasks"):
            await self._accept_sync_pack(v, pack)
        rw = reply.get("work")
        if isinstance(rw, dict) and rw.get("kind") == "riven_gain":
            await self._accept_sync_riven(v, rw)
        nxt = reply.get("next_ms")
        try:
            # Floor at 1 s: a 0/negative (or a malformed) `next_ms` must not
            # make the verifier due on every 5 s pass.
            self._sync_next[vid] = now + max(1.0, float(nxt)) / 1000.0
        except (TypeError, ValueError):
            pass
        self._sweep_board_kept(now)
        # `taken`/`dropped` just settled every answer that was owed: a pack
        # with nothing left is finished, and closing it here (not on the next
        # watchdog pass) is what lets the verifier ask for the next one at
        # once instead of sitting on two spent leases.
        await self._close_finished_sync_packs(v)
        return "ok"

    async def _accept_sync_pack(self, v: Verifier, pack: dict) -> None:
        async with self._deliver_lock(v.vid):
            await self._accept_sync_pack_locked(v, pack)

    async def _accept_sync_pack_locked(self, v: Verifier, pack: dict) -> None:
        work = work_from_response(None, pack, source="sync")
        if work is None:
            self.log.event("sync_pack_unparseable", verifier=v.vid,
                           lease=str(pack.get("lease") or ""))
            return
        if self.paused or self.maintenance or not self._verifier_on_duty(v):
            # A SYNC SENT BEFORE THE PAUSE — or before this device went off
            # duty (beyond `active_verifiers`, or a spare standing down) — can
            # still answer afterwards.  Taking the pack would mean computing
            # rows nobody is going to renew, and its rows then come home one by
            # one as `held:false`.  Hand it straight back instead.
            self.log.event("sync_pack_refused", verifier=v.vid,
                           lease=work.lease_id, tasks=len(work.tasks),
                           reason=("paused" if self.paused else "maintenance")
                           if (self.paused or self.maintenance) else "off duty")
            self._spawn(self._release_lease(v, work))
            return
        self.log.event("fetch_work", verifier=v.vid, ahead=False,
                       work_kind=work.kind, tasks=len(work.tasks),
                       lease=work.lease_id, source="sync")
        if v.lease is None:
            v.lease = work
            v.fetched += len(work.tasks)
            v.sync_state("sync_pack")
        elif v.queued is None:
            v.queued = work
            v.fetched += len(work.tasks)
            v.sync_state("sync_pack_queued")
            self.record_slot_next(v, work)
            self.log.event("queue_recorded", verifier=v.vid,
                           work_kind=work.kind, lease=work.lease_id,
                           tasks=len(work.tasks), source="sync")
            # NO EXTRA HAND-IN SYNC HERE.  The rule is satisfied the moment
            # `v.queued` is set, and the answers will ride the NEXT sync; that
            # costs one pacing interval of CONFIRMATION, not of compute — the
            # queued pack is picked up by the engines straight away (both packs
            # are usable as soon as they are held), so nothing idles while the
            # hand-in waits for the tick.
        else:
            self.log.event("sync_pack_no_room", verifier=v.vid,
                           lease=work.lease_id, tasks=len(work.tasks))
            self._spawn(self._release_lease(v, work))
            return
        if v.lease is not None and v.queued is not None:
            await self._maybe_claim(v)
        self.on_event("fetch", f"{v.label} <- {work.kind} "
                               f"{len(work.tasks)} row(s)（sync）")

    async def _accept_sync_riven(self, v: Verifier, rw: dict) -> None:
        work = work_from_response(dict(rw), None, source="sync")
        if work is None:
            self.log.event("sync_riven_unparseable", verifier=v.vid,
                           code=str(rw.get("code") or ""))
            return
        self.log.event("fetch_work", verifier=v.vid, ahead=False,
                       work_kind=work.kind, tasks=len(work.tasks),
                       lease=work.lease_id, source="sync")
        if self.paused or self.maintenance or not self._verifier_on_duty(v):
            self.log.event("sync_pack_refused", verifier=v.vid,
                           lease=work.lease_id, tasks=len(work.tasks),
                           work_kind="riven_gain",
                           reason=("paused" if self.paused else "maintenance")
                           if (self.paused or self.maintenance) else "off duty")
            self._spawn(self._release_lease(v, work))
            return
        if self._board_only(v) or not self._riven_wanted():
            # BOARD ONLY: hand it straight back.  `release: true` is the
            # documented door for a riven (`worker/appraise.js` → `facts().
            # release()`, which the doc says leaves the asker "not left behind
            # a lease nobody works"), and it writes no refusal, so the account
            # is not marked — the same question may come back to this client,
            # which is why the queue above is what actually keeps rivens away
            # and this is only the pool-is-empty fallback.
            #
            # The server sets this verifier's `next_at` to `now + sync_floor_ms`
            # (60 s) when it hands a question out alone, so every ask before
            # that is answered `early` and hands out nothing: wait
            # `RIVEN_REFUSE_RETRY_S` (70 s) before asking again.
            now = time.time()
            v.riven_cooldown_until = now + RIVEN_REFUSE_RETRY_S
            self._sync_next[v.vid] = max(
                float(self._sync_next.get(v.vid, 0.0) or 0.0),
                v.riven_cooldown_until)
            reason = "board_only" if self._board_only(v) else "riven cooldown"
            self.log.event("sync_pack_refused", verifier=v.vid,
                           lease=work.lease_id, tasks=len(work.tasks),
                           work_kind="riven_gain", reason=reason,
                           cooldown_s=RIVEN_REFUSE_RETRY_S)
            self.on_event("deadline",
                          f"{v.label} 只做榜单：退回紫卡 {work.code}，"
                          f"{int(RIVEN_REFUSE_RETRY_S)} 秒冷却后再申请")
            self._spawn(self._release_lease(v, work))
            return
        if v.lease is None:
            v.lease = work
            v.fetched += len(work.tasks)
            v.sync_state("sync_riven")
        elif v.queued is None:
            v.queued = work
            v.fetched += len(work.tasks)
            v.sync_state("sync_riven_queued")
            self.record_slot_next(v, work)
        else:
            self.log.event("sync_riven_no_room", verifier=v.vid,
                           lease=work.lease_id)
            self._spawn(self._release_lease(v, work))
            return
        if v.lease is not None and v.queued is not None:
            await self._maybe_claim(v)
        self.on_event("fetch", f"{v.label} <- riven_gain（sync）")

    async def _sync_final_round(self, hold_inflight: bool) -> int:
        """Results-carrying, work-releasing syncs, e.g. for stop/pause.

        `hold_inflight=False` (stop): name nothing, so every live lease goes
        back; results still land.  `hold_inflight=True` (pause): in-flight
        rows AND rows whose answer is still owed stay named (they are the
        ones this round is delivering), everything unstarted goes back.
        """
        if not self._board_sync_active():
            return 0
        lanes = int(self.cfg.get("work_lanes") or 16)
        pace = board_sync.clamp_pace_ms(self.pace_ms, self._sync_dials)
        synced = 0
        for v in list(self.verifiers):
            if v.vid is None:
                continue
            if hold_inflight:
                by_lease: dict[str, list] = {}
                for work, _role in v.works():
                    if work is None or work.aborted or work.kind != "board":
                        continue
                    # in-flight rows finish afterwards and sync again; a
                    # finished row whose answer this round carries has to
                    # stay named too, or the release that same request asks
                    # for beats its own `results` to the row (`not_held`).
                    rows = [t.question for t in work.tasks
                            if t.question and not t.lost
                            and (t.state == "inflight"
                                 or (work.lease_id, t.question)
                                 in self._board_kept)]
                    if rows:
                        by_lease.setdefault(work.lease_id, []).extend(rows)
                held = [{"lease": l, "rows": r} for l, r in by_lease.items()]
            else:
                held = []
            pending = self._sync_pending_board_rows(v)
            results = self._sync_results_for(v.vid)
            self._mark_answers_sent(results)
            refused = self._sync_refused.pop(v.vid, [])
            # A verifier that merely holds unstarted rows (no inflight, no
            # results, no refused) still needs to report/renew them on the
            # final round, otherwise pause/stop leaves them unnamed.
            if not held and not pending and not results and not refused:
                continue
            body = board_sync.build_sync_body(
                verifier=v.vid, engine=self.engine_id or "",
                consent_v=CONSENT_V, consent_at=utc_iso(),
                lanes=lanes, pace_ms=pace, want_ms=0, held=held,
                results=results, refused=refused or None,
                idle=board_sync.idle_payload(self._sync_idle, v.vid) or None)
            try:
                out = await self._io(self.remote.board_sync, body)
            except Exception:
                self._sync_refused[v.vid] = (
                    refused + self._sync_refused.get(v.vid, []))
                continue
            if not isinstance(out, dict) or not out.get("ok"):
                self._sync_refused[v.vid] = (
                    refused + self._sync_refused.get(v.vid, []))
                continue
            synced += 1
            await self._apply_sync_reply(v, out, 0)
        return synced

    async def _sync_board_round(self) -> None:
        """One pass over the verifiers; each due id syncs at most once.

        The per-verifier syncs are independent (separate leases, separate
        servers from the sync loop's point of view), so they are awaited
        together rather than serialised.
        """
        if not self.running or not self._board_sync_active():
            return
        now = time.time()
        tasks = []
        for v in list(self.verifiers):
            if v.vid is None:
                continue
            tasks.append(asyncio.ensure_future(self._sync_verifier_round(v, now)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._persist_task_copy()

    def _persist_task_copy_guard(self) -> None:
        self._persist_task_copy()

    async def _sync_verifier_round(self, v: Verifier, now: float) -> None:
        try:
            due, reason, want = await self._sync_due(v, now)
        except Exception as e:
            self.log.event("sync_due_error", verifier=v.vid,
                           error=f"{type(e).__name__}: {e}")
            return
        if not due:
            return
        try:
            await self._sync_verifier(v, reason, want)
        except Exception as e:
            self.log.event("sync_error", verifier=v.vid, reason=reason,
                           error=f"{type(e).__name__}: {e}")

    async def sync_loop(self, every: float = 5.0) -> None:
        """Protocol-8 scheduler.  Each sync paces itself (`next_ms`); this loop
        only notices dues, and idles cheaply between them.

        IT WAKES FOR THE EARLIEST DUE, not on the fixed grid `every`.  `next_ms`
        is the server saying when this id may next be handed work; asking before
        that is answered `early: true` and buys nothing, and asking on a 5 s
        grid overshoots the cooldown by up to 5 s — which is the whole cooldown
        wasted whenever the server is handing out short packs.  So the sleep is
        computed from the pacing the server itself returned.
        """
        while self.running:
            try:
                await self._sync_board_round()
            except Exception as e:
                self.log.event("sync_loop_error",
                               error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(self._until_next_sync_due(every))

    def _until_next_sync_due(self, cap: float) -> float:
        """Seconds to sleep before the next verifier's pacing tick.

        Only verifiers that are actually WAITING FOR WORK tighten the loop:
        one holding a lease, a queued pack, unsent answers or a fetch in
        flight.  An idle client keeps the plain `cap` cadence and is driven by
        the work signal instead.  The floor keeps the loop from spinning when
        a verifier is due but `_sync_due` declines to send.
        """
        now = time.time()
        soonest = cap
        for v in self.verifiers:
            if v.vid is None or not self._verifier_on_duty(v):
                continue
            if not (v.lease or v.queued or self.fetch_tasks.get(v.vid) is not None
                    or self._has_unsent_board(v)):
                continue
            nxt = float(self._sync_next.get(v.vid, 0.0) or 0.0)
            soonest = min(soonest, (nxt - now) if nxt > now else 0.25)
        return max(0.1, min(cap, soonest))

    async def _poll_task_statuses(self) -> None:
        for v in list(self.verifiers):
            orders = self.task_status.open_orders(v.vid)
            tasks = self.task_status.open_riven_tasks(v.vid)
            if not orders and not tasks:
                continue
            try:
                await self._poll_verifier_status(v, orders, tasks)
            except Exception as e:
                import traceback as _tb
                self.log.event("task_status_error", verifier=v.vid,
                               error=f"{type(e).__name__}: {e}",
                               trace=_tb.format_exc(limit=8))

    async def _poll_verifier_status(self, v: Verifier, orders: list[dict],
                                    tasks: list[dict]) -> None:
        now = time.time()
        # `/api/board/mine` WAS A PROTOCOL-7 DOOR AND IS NOT CALLED AT ALL.
        # Under protocol 8 its ledger view does not know what a sync just took:
        # a run answered and recorded through `results` came back
        # `state:"gone"` here (measured — every order of one poll read `gone`,
        # and the very next sync replied `taken` for the same questions), and
        # believing it re-sent already-recorded answers through `/answers` and
        # swung the invalid tally.  The sync reply's own
        # `taken`/`dropped`/`states` is the only oracle now.
        #
        # The RIVEN door below is NOT protocol 7's: `appraise/mine` exists in
        # both wires and is how a riven's own state is read.
        if tasks:
            out = await self._io(self.remote.appraise_mine, v.vid, tasks)
            if isinstance(out, dict) and out.get("ok"):
                self.task_status.apply_appraise_mine(v.vid, out, now)
                self.log.event("task_status", verifier=v.vid, work_kind="riven",
                               queried=len(tasks), returned=len(out.get("tasks") or []))
            else:
                self.log.event("task_status_error", verifier=v.vid, work_kind="riven",
                               response=out)

    def _set_leaderboard_error(self, r) -> None:
        self.leaderboard_error = str((r or {}).get("error")
                                     or (r or {}).get("http_error")
                                     or (r or {}).get("transport_error")
                                     or "leaderboard unavailable")

    def _leaderboard_row(self, c: dict, now: float):
        try:
            key = str(c.get("key") or "")
            points = int(c.get("points") or 0)
            if not key:
                return None
            if key not in self.leaderboard_base:
                self.leaderboard_base[key] = (points, now)
            base, base_at = self.leaderboard_base[key]
            elapsed_min = max(1.0 / 60.0, (now - base_at) / 60.0)
            rate = max(0.0, (points - base) / elapsed_min)
            return {"key": key, "name": c.get("name"), "points": points,
                    "rate": rate, "volunteer": bool(c.get("volunteer"))}
        except Exception:
            return None

    def _apply_leaderboard(self, r: dict, now: float) -> None:
        rows = []
        for c in list(r.get("contributors") or []):
            row = self._leaderboard_row(c, now)
            if row is not None:
                rows.append(row)
        rows.sort(key=operator.itemgetter("points"), reverse=True)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
        self.leaderboard = rows
        self.leaderboard_computing = int(r.get("computing") or 0)
        self.leaderboard_total = int(r.get("total") or len(rows))
        self.leaderboard_at = now
        self.leaderboard_error = ""

    async def _refresh_leaderboard(self) -> None:
        """Read the public ranking and compute this run's points/minute.

        THE RANKING IS A TEN-MINUTE FILE, NOT A MINUTE'S CALL: the worker
        rewrites `stats/contributors.json` every `stats_contributors_ms` (whose
        default AND minimum are both 10 minutes), `/api/contributors` reads that
        file back for up to two periods, and the edge holds the answer a further
        60 seconds -- `worker/contribution.js`, `worker/dials.js`,
        docs/BOARD.md §"The public stats".  So this is asked every two minutes:
        ahead of the data, and not one request per reader per minute for an
        answer that cannot have moved.  (The number that does move each minute
        is the tally on the home page, which this client does not show.)  A
        contributor's baseline is taken when that key is first seen in this
        process; a later row therefore cannot borrow points earned before the
        client opened.
        """
        r = await self._io(self.remote.contributors, 100)
        if not isinstance(r, dict) or not r.get("ok"):
            self._set_leaderboard_error(r)
            return
        self._apply_leaderboard(r, time.time())

    async def leaderboard_loop(self, every: float = 120.0) -> None:
        # TWO MINUTES, NOT ONE: the ranking is republished every ten (see
        # `_refresh_leaderboard`), so a minute's cadence was six reads of the
        # same bytes.  The first read still lands immediately, before the sleep.
        while self.running:
            try:
                await self._refresh_leaderboard()
            except Exception as e:
                self.leaderboard_error = f"{type(e).__name__}: {e}"
                self.on_event("error", f"leaderboard: {self.leaderboard_error}")
            await asyncio.sleep(every)

    # ------------------------------------------------------------------ loop
    async def _drop_expired_work(self, v: Verifier, work, reason: str) -> None:
        if work.kind == "board":
            await self._finalize_board_deadline(v, work)
            return
        await self._give_back_work(v, work, reason)

    async def _expire_lease(self, v: Verifier, work, now: float) -> None:
        if self.renew_supported is False:
            if work.expired():                    # old server
                await self._drop_expired_work(v, work, "lease expired")
            return
        if work.hard_deadline and now >= work.hard_deadline:
            self.on_event("deadline", f"{v.label} 续约长期失败，按截止处理")
            await self._drop_expired_work(v, work, "renew failing")
            return
        if work.kind == "board" and not work.live_tasks():
            await self._give_back_work(v, work, "all rows lost")

    async def _expire_one(self, v: Verifier) -> None:
        now = time.time()
        for work, _role in list(v.works()):
            if work.aborted or work.all_done():
                continue
            if work.kind == "board" and self._sync_source(work):
                continue           # the sync loop owns sync packs
            if not work.is_live(now):
                await self._drop_expired_work(v, work, "lease gone")
                continue
            await self._expire_lease(v, work, now)

    async def _expire_round(self) -> None:
        for v in list(self.verifiers):
            await self._expire_one(v)

    def _run_spare_tick(self) -> None:
        try:
            self._spare_evaluate(time.time())
        except Exception as e:
            self.log.event("spare_error", error=f"{type(e).__name__}: {e}")

    async def run(self) -> None:
        """The 0.25 s scheduling beat: ONLY the cheap, synchronous pickers.

        Everything that talks to the network — heartbeats, lease expiry, the
        pending-riven re-send — runs in its own loop.  One slow request
        (`Remote` waits up to 120 s) used to hold this beat, so no fetch and no
        renewal happened for minutes while a dead connection was waited out.

        NO FETCH ROUND IS CALLED HERE ANY MORE.  Both `_fetch_plain_round` and
        `_fetch_ahead_round` asked the classic `/api/board/work` door, and that
        door is gone: protocol 8's sync reply is the only place work arrives.
        """
        self._run_spare_tick()

    async def heartbeat_loop(self, every: float = 0.5) -> None:
        """Keep every live lease alive, off the fetch beat."""
        while self.running:
            try:
                await self._heartbeats()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_event("error", f"heartbeat: {type(e).__name__}: {e}")
            await asyncio.sleep(every)

    async def expire_loop(self, every: float = 1.0) -> None:
        """Drop leases that lapsed, off the fetch beat."""
        while self.running:
            try:
                await self._expire_round()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_event("error", f"expire: {type(e).__name__}: {e}")
            await asyncio.sleep(every)

    async def pending_riven_loop(self, every: float = 1.0) -> None:
        """Re-send finished riven answers, off the fetch beat.

        `_retry_pending_riven` throttles itself to one pass every
        `RIVEN_PENDING_EVERY_S`; this loop only decides when it may look.
        """
        while self.running:
            try:
                await self._retry_pending_riven()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_event("error", f"pending riven: {type(e).__name__}: {e}")
            await asyncio.sleep(every)

    def _verifier_snapshot(self, v: Verifier) -> dict:
        snap = v.snapshot()
        pending = self.task_status.pending_points(v.vid)
        snap["pending_points"] = pending
        snap["session_pending"] = pending
        snap["task_counts"] = self.task_status.counts(v.vid)
        # THE QUEUE COLUMN: is a second pack in hand, and if not, when does the
        # next ask go out.  Nothing here gates the hand-in.
        snap["queue_empty"] = self._queue_empty(v)
        nxt = float(self._sync_next.get(v.vid, 0.0) or 0.0)
        snap["ask_in_s"] = int(max(0.0, nxt - time.time())) if nxt > 0 else 0
        return snap

    def _snapshot_stats(self, task_counts: dict) -> dict:
        stats = dict(self.stats)
        stats.update({
            "task_pending": task_counts["pending"],
            "task_waiting": task_counts["waiting"],
            "task_checking": task_counts["checking"],
            "task_confirmed": task_counts["confirmed"],
            "task_gone": task_counts["gone"],
            "task_local_invalid": task_counts["local_invalid"],
            "task_invalid": task_counts["invalid"],
            "task_returned": task_counts["returned"],
            "invalid": task_counts["invalid"],
            "returned": task_counts["returned"],
        })
        return stats

    def _snapshot_core(self) -> dict:
        return {
            "engine": self.engine_id, "release": self.release,
            "local_engine": self.local_engine,
            "wire_engine": self.wire_engine,
            "served_engine": self.served_engine,
            "engine_catalog": self.engine_catalog,
            "forced_task": self.forced_task,
            "cpu_limit": int(self.cfg.get("cpu_limit_percent") or 100),
            "renew_supported": self.renew_supported,
            "cpu_limit_actual": self.cpu_limit_actual,
            "core_type": str(self.cfg.get("core_type") or "auto"),
            "core_type_note": self.core_type_note,
            "maintenance": self.maintenance,
            "uptime": time.time() - self.t0,
        }

    def _snapshot_pools(self) -> dict:
        return {
            "active": self.active_count(), "max": int(self.cfg["max_verifiers"]),
            "pool_riven": self.pool_riven(), "pool_board": self.pool_board(),
            "mode": "riven" if self.riven_slots else "board",
            "riven_slots": sorted(self.riven_slots),
            "riven_total": self.riven_total(),
            "drained_lanes": self.drained_lanes,
            "stopped_slots": sorted(self.stopped_slots),
            "fetching_count": self.fetching_count,
            "slot_lanes": list(self.slot_lanes),
            "base": int(self.cfg.get("base_verifiers") or 0),
            "tasks_computing": self.computing_count(),
            "tasks_holding": self.holding_count(),
            "tasks_queued": self.queued_count(),
            "tasks_total": self.total_tasks(),
            "tasks_capacity": self.max_tasks(),
            "verifiers_claimed": sum(1 for v in self.verifiers if v.account_claimed),
            "verifiers_unclaimed": sum(1 for v in self.verifiers if not v.account_claimed),
            "queued_tasks": self.queued_tasks(),
            "slots_busy": {k: v for k, v in self.slot_task.items()},
        }

    def _snapshot_verifiers(self, shown: list, task_counts: dict) -> dict:
        return {
            "stats": self._snapshot_stats(task_counts),
            "account_points": self.account_points,
            "account_signed_in": self.account_signed_in,
            "points_pending": self.points_now()[0],
            "points_confirmed": self.points_now()[1],
            "points_per_minute": self.points_per_minute(),
            "leaderboard": list(self.leaderboard),
            "leaderboard_computing": self.leaderboard_computing,
            "leaderboard_total": self.leaderboard_total,
            "leaderboard_at": self.leaderboard_at,
            "leaderboard_error": self.leaderboard_error,
            # ONLY THE VERIFIERS IN USE.  `_drop_verifier` lowers
            # `max_verifiers` and lets the last one finish what it holds, so
            # the full list would keep showing a row the user just removed.
            "verifiers": [self._verifier_snapshot(v) for v in shown],
            "slots": [st.snapshot() for st in self.slots],
            "uploading": [st.uploading for st in self.slots if st.uploading],
        }

    def snapshot(self) -> dict:
        shown = self.verifiers[:int(self.cfg["max_verifiers"])]
        task_counts = self.task_status.counts_of([v.vid for v in shown])
        snap = {}
        snap.update(self._snapshot_core())
        snap.update(self._snapshot_pools())
        snap.update(self._snapshot_verifiers(shown, task_counts))
        return snap
