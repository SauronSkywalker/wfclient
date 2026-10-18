# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Everything the client remembers, in one JSON file beside it."""
from __future__ import annotations
import io, json, os

import paths

ROOT = paths.ROOT
PATH = paths.CONFIG

#: HOW MANY COMPUTE PROCESSES (slots) this client runs.  Three by default; the
#: settings page moves it one at a time between one and ten, and every change
#: goes through the pause/drain/restart flow.  The count feeds
#: `_plan_lanes`/`_start_engines`/`_spawn_loops`, never a literal 3.
SLOTS_DEFAULT = 3
SLOTS_MIN = 1
SLOTS_MAX = 10


def clamp_slots(value) -> int:
    """A usable compute-process count: 1..10, defaulting to 3."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = SLOTS_DEFAULT
    return max(SLOTS_MIN, min(SLOTS_MAX, n))

DEFAULTS = {
    "account": {"email": "", "password": ""},
    #: verifier ids already claimed for this account, in activation order
    "verifiers": [],
    #: verifier ids already claimed for this account (claim once, not every run)
    "claimed": [],
    #: how many are allowed to fetch at once, and how many may ever be claimed.
    #: Under protocol 7 a verifier can hold its current work plus one ahead work;
    #: the compute slots still determine how many rows actually run at once.
    "active_verifiers": 6,
    "base_verifiers": 6,
    "max_verifiers": 8,
    #: minimum allowed verifier count; one verifier may hold current + ahead work
    "min_verifiers": 1,
    #: at most this many verifiers may be in FETCHING at the same time
    "fetch_parallel": 2,
    #: compute slots, each with its own engine process and its own cores
    "slots": 3,
    #: "auto" splits the scanned logical CPU count across slots (for example
    #: 16 logical CPUs over 3 slots -> 5,5,6).  A positive integer remains a
    #: fixed per-slot override.
    "lanes_per_slot": "auto",
    #: lanes reported to /api/board/work.  This is a scheduling/eligibility
    #: claim, not the number of compute threads.  Keep it separate from
    #: lanes_per_slot so the server sees a full-width machine while the local
    #: engines still use the real per-slot thread budget.
    "work_lanes": 16,
    #: /api/board/work request timeout. More than this is treated as a
    #: transport/server error and the verifier enters its cool-down.
    "work_timeout_seconds": 30,
    #: pull the next pack only when the current one is nearly spent; this
    #: avoids holding two full 20-row packs while three engines compute.
    #: A board pack this small is asked for its successor immediately — a
    #: single row or a 5-8 row pack is over before a round trip matters.
    "board_small_pack_rows": 8,
    #: Bigger packs wait for this fraction of their rows, TRUNCATED: 16 rows
    #: -> floor(12.8) -> the next work is asked once 12 are done.
    "board_ahead_at": 0.8,
    #: Before any riven has finished there is no "last one took this long" to
    #: measure against, so this stands in for it.
    "riven_first_ahead_s": 120,
    "ahead_floor_tasks": 3,
    "ahead_remaining_ms": 15000,
    #: first pace_ms before any board row has been measured; the server uses it
    #: to size the first pack and the client updates it with an EWMA.
    "pace_ms_default": 1000,
    #: request the next riven only after the real optimizer progress reaches
    #: this fraction; below it the running riven keeps all released lanes.
    "riven_ahead_at": 0.80,
    #: when true, a verifier that already holds one work may fetch one extra
    #: with `ahead:true`; board work is a pack, riven work is one task.
    "single_verifier_queue": True,
    #: upload board answers in batches of at most two instead of waiting for
    #: the whole pack.  The server still receives the final batch with done=true.
    "small_batch_upload": True,
    #: THE FORCED QUEUE — and, since protocol 8, THE ONE SWITCH FOR WHAT THIS
    #: CLIENT COMPUTES.  It used to mean "hand nothing in until the next work
    #: is already held" and only drove the classic `ahead:true` door (retired,
    #: so under protocol 8 it did nothing at all).
    #: 1 = BOARD ONLY: keep a pack queued at all times — the server never hands
    #:     a question someone waits on to a computer that still holds a row
    #:     (worker/sync.js `holds.questions > 0` → `idle`) — and if a riven
    #:     arrives anyway (the pool had nothing else), give it straight back
    #:     and stay off that door for `RIVEN_REFUSE_RETRY_S` (70 s, clearing the
    #:     server's own 60 s cooling after a hand-out `alone`).
    #: 0 = BOTH: no forced queue — a verifier holds one work at a time, exactly
    #:     as the official page runs — so rivens can reach it, and a riven in
    #:     hand is computed to the end with the sync held back while it runs,
    #:     so the unnamed-release rule cannot take its lease away.
    "queue_fallback": 0,
    #: HOW MANY FINISHED BOARD ROWS A PACK HAND IN AT ONCE, while the pack is
    #: still being computed.  The whole pack used to go in as one batch at the
    #: end, and that is what made a slow hand-in expensive: the rows waited for
    #: the pacing tick, and a row another independent client answers first is
    #: settled out from under us (`dropped: {why: "not_held"}`, docs/CLIENTS.md
    #: "the first answer settles it").  Ten at a time keeps the exposure to the
    #: time it takes to compute ten.
    "board_hand_in_rows": 10,
    #: HOW LONG THE PACK'S LAST ROW MAY BE KEPT BACK FOR A QUEUED PACK, in
    #: milliseconds — the "anchor".
    #:
    #: Everything else in a pack is handed in as it finishes; exactly ONE row is
    #: kept unanswered so the id is never holding nothing — the server hands a
    #: riven only to an id holding nothing, and board-only mode then refuses it
    #: and pays 70 s of cool-down.  Keeping one means the worst case is ONE row
    #: lost to another holder, instead of the whole pack.
    #:
    #: ONE HOUR, and it ends early for one reason only: **the anchor was taken
    #: from us**.  A row another independent client answers first is settled out
    #: from under us (`held: false`, `dropped: {why: "not_held"}`); with nothing
    #: left to renew there is no anchor any more, so the whole pack goes and the
    #: id can be handed the next one.  That is what keeps the wait from
    #: wedging: a pack whose anchor is gone cannot stay open.
    #:
    #: `min(this, the server's row_ceiling_ms)` is the value in force.  The
    #: hour is deliberately well inside `row_ceiling_ms` (2 h), past which
    #: another client may take an outstanding row anyway.
    #: HOW MUCH WORK A BOARD ASK ASKS FOR, in milliseconds of this client's own
    #: pace (`want_ms`, docs/CLIENTS.md §Values).  0 — the default — asks for
    #: `pack8_ms`, the biggest pack the server hands, and that is what the
    #: reference session ran: 62.5 rows a pack and a MEDIAN 463 s between a row
    #: arriving and an engine touching it (see fleet._board_want_ms).  A smaller
    #: value buys shorter exposure and a pack that closes sooner, at the cost of
    #: a sync round trip per pack (0.7-3.5 s) on the client's own CPU.
    "board_want_ms": 0,
    "board_submit_hold_ms": 3600000,
    #: 2.4 IDLE RECYCLE: give an idle engine's memory back after this many
    #: seconds with nothing to take (`fleet._slot_loop` -> `_idle_release`).
    #: Batches arrive minutes apart — a MEDIAN 463 s between a row arriving and
    #: an engine touching it — and an engine that is merely idle still holds its
    #: loaded tables (42-61 MB measured).  0 — the default — turns the recycle
    #: off.  60 is the value the plan was written against; the wake path
    #: (`_idle_wake`) restarts the engine BEFORE the row that ended the spell is
    #: computed, so the cost is one engine start, never a stalled row.
    "idle_recycle_s": 0,
    #: 2.4: how many engines stay up while the fleet is idle.  Never below 1 —
    #: a client that has reclaimed every engine pays a start-up stall on the
    #: very next row, and the plan's floor was one warm engine.
    "idle_keep": 1,
    #: 2.2 ENGINE-SIDE BATCHING: board rows per engine call (1 = one row a call,
    #: the original path).  `POST /api/board/rows` pipelines N rows inside one
    #: call — each row's serial `score` overlaps the next row's fold — measured
    #: 3.9% faster on 30 rows, bit-identical.  The ENGINE must carry the route:
    #: a build without it answers 404, the batch fails as a whole, and the fleet
    #: falls back to one row a call (`board_batch_failed`).
    "board_batch_rows": 1,
    #: A ROW THAT HAS WAITED THIS LONG FOR AN ENGINE goes to the front of the
    #: pick order, level with a row the reader pointed at: renewals keep a row
    #: alive forever, so without this a pack that arrives faster than it drains
    #: can starve one row until the server's own row ceiling takes it.  0 turns
    #: the promotion off; delete the key and the 40-minute default stands
    #: (fleet.py `STALE_START_S`).
    "stale_start_promote_seconds": 2400,
    #: Manual terminal zoom, in percent: 0 = follow Windows, otherwise one of
    #: 50/75/100/125/150/175/200.  The TUI cannot scale its own glyphs, so this
    #: changes the current Windows Terminal profile's font size (with backup);
    #: `ui_scale_state` stores the original size so "follow" can restore it.
    "ui_scale_percent": 0,
    "ui_scale_state": {},
    #: JSONL debug logging, TWO levels, and the default is "off" (关) — an
    #: untouched install writes nothing at all.  "full" (完整) keeps every body
    #: and the CPU-scheduling records (affinity masks, CPU Sets, per-row cores
    #: used, per-slot idle spells).  Names from older builds that had middle
    #: levels ("standard"/"verbose"/"summary", or a bare true) all read as
    #: "full" — they all meant "write something".
    "task_logging": "off",
    # "" = let the package decide: the self-compiled line defaults to "notify"
    # (detect and say so, replace nothing until asked), a prebuilt zip to
    # "auto".  The settings page stores an explicit "auto" / "notify".
    "update_mode": "",
    #: persist board/riven task answers & results to disk between runs.
    #: Default false: answers stay in memory only (see requirements).
    "task_persist": False,
    #: hard CPU ceiling for the three engine processes, in 10% steps. 100
    #: leaves the engine alone; lower values use a Windows Job Object cap.
    "cpu_limit_percent": 100,
    #: slot watchdog: 10 s memory checks; an idle slot with pending work is
    #: restarted after 30 s.  Normal checks never touch the network.
    "slot_watchdog_seconds": 10,
    "slot_idle_restart_seconds": 30,
    #: verifier watchdog: a verifier that can no longer ask (neither idle nor
    #: holding a live lease) is reset and told to fetch again.  0 disables it.
    "verifier_watchdog_seconds": 10,
    #: slot stall watchdog: a slot whose engine process adds no CPU time for
    #: this many samples is judged stuck and rebuilt.  A genuinely computing
    #: engine always accumulates CPU, so a slow but working row is safe.
    "slot_stall_seconds": 60,
    "slot_stall_checks": 3,
    #: how long a finished, undelivered riven answer may hold its verifier.
    #: 0 disables the cap.
    "riven_pending_hold_seconds": 900,
    "engine_exe": "engine\\wfsim-core.exe",
    "engine_base_port": 9310,
    #: which logical CPUs the engine processes may run on. Empty = all,
    #: i.e. the scheduler decides.
    "engine_cores": [],
    #: fine-grained GitHub PAT; empty means anonymous GitHub API requests.
    #: The user edits this in config.json.  It is never printed or logged.
    "github_pat": "",
    #: last engine id served, used when /release.json is rate-limited
    "known_engine": "",
    #: engine id reported to the server. Empty = follow the installed core
    #: (i.e. always the latest once updated).  Picking the served/current id
    #: in settings stores "" rather than the id, so a choice that "was the
    #: latest when picked" keeps tracking releases instead of going stale.
    #: A manually selected OLDER id stays explicit and is accepted only while
    #: release.json.accepted still lists it; switching always drains first.
    "wire_engine": "",
    #: the 手动 switch beside the engine ± buttons, OFF by default.  The engine
    #: id above is read ONLY while this is on: with it off the client always
    #: reports the core it really installed, so a data/ folder copied from an
    #: older package can no longer pin a stale id, and the header cannot
    #: advertise a version this machine is not running.
    "wire_manual": False,
    #: board wire: protocol 8, always.  There is no switch any more — one
    #: door, one writer.  The server can still force protocol 7 for a
    #: session (`fallback: 7`, docs/CLIENTS.md) and the client honours it.
    #: Older configs' board_protocol/client_protocol keys are dropped on load.
    #: where the CLIENT itself runs: "auto" pins this process to the least
    #: performant core (one SMT thread when the core has siblings, the whole
    #: core when it does not) and keeps that thread out of the engine pool;
    #: "off" leaves the client on every core; a number pins that logical
    #: processor explicitly.  Takes effect at start-up.
    "log_cpu": "auto",
    "consent_version": 3,
}

#: Settings that no longer exist.  Board work is protocol 8 only (one door);
#: the in-app protocol switch and the protocol-7 default were retired on
#: 10-14, and an old config.json carrying them is migrated, not honoured.
DROPPED_KEYS = ("board_protocol", "client_protocol", "riven_pending_max")

def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(PATH):
        try:
            with io.open(PATH, encoding="utf-8") as fh:
                saved = json.load(fh)
            for k, v in saved.items():
                if k == "account" and isinstance(v, dict):
                    cfg["account"].update(v)
                elif k in DROPPED_KEYS:
                    continue          # retired settings: never resurrect them
                else:
                    cfg[k] = v
        except Exception:
            pass
    for k in DROPPED_KEYS:
        cfg.pop(k, None)
    cfg["slots"] = clamp_slots(cfg.get("slots"))
    return cfg


def default_config() -> dict:
    """A fresh copy of the built-in defaults."""
    return json.loads(json.dumps(DEFAULTS))


def detected_cpus() -> list[int]:
    """Logical CPUs in the current Windows CPU-set order.

    ``cores.topology()`` is the same source the cores page uses, so the first
    launch cannot accidentally inherit CPU numbers from the machine that made
    the portable package.
    """
    try:
        import cores
        found = sorted({int(item["cpu"]) for item in cores.topology()
                        if isinstance(item, dict) and "cpu" in item})
        if found:
            return found
    except Exception:
        pass
    try:
        return list(range(max(1, int(os.cpu_count() or 1))))
    except Exception:
        return [0]


def _pick_engine_base_port(start: int = 9310, stop: int = 9399) -> int:
    """Pick a local port range that is free on this machine right now."""
    import socket
    for port in range(int(start), int(stop) + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    return int(start)


def machine_default_config() -> dict:
    """Defaults that reflect this machine instead of the packaging machine."""
    cfg = default_config()
    cpus = detected_cpus()
    total = len(cpus)
    try:
        import cores
        total = max(total, len(cores.cpu_sets()))
    except Exception:
        pass
    cfg["engine_cores"] = cpus
    cfg["slots"] = clamp_slots(cfg.get("slots"))
    # The server accepts a lane claim up to 256; beyond that it treats the
    # client as one lane.  The engine may still use more local threads.
    cfg["work_lanes"] = max(1, min(256, total or len(cpus)))
    cfg["engine_base_port"] = _pick_engine_base_port()
    return cfg


def load_or_create() -> tuple[dict, bool]:
    """Load config.json, creating a machine-specific default when absent."""
    if os.path.exists(PATH):
        return load(), False
    cfg = machine_default_config()
    save(cfg)
    return cfg, True


def save(cfg: dict) -> None:
    paths.ensure_data()
    io.open(PATH, "w", encoding="utf-8").write(json.dumps(cfg, indent=2, ensure_ascii=False))
