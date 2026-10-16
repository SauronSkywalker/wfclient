# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Protocol-8 board sync helpers (dual-stack with protocol 7).

The wire shape follows upstream `docs/CLIENTS.md` §"The cycle, protocol 8":
one `POST /api/board/sync` carries everything the id holds (`held`), the
answers ready (`results`), refusals, idle counts and state queries, and the
reply confirms (`taken`/`dropped`/`held`), hands out (`pack`/`work`), paces
(`next_ms`) and re-dials (`dials`).

Everything here is pure logic over plain dicts so it unit-tests without a
fleet: the fleet owns the state (leases, kept answers, timers) and calls in.
Network for the work signal is the one exception (`fetch_work_signal`), kept
here so the polling policy lives beside the shapes it serves.
"""
from __future__ import annotations

import json
import random
import time
import urllib.request

SYNC_PROTOCOL = 8
SYNC_BODY_MAX = 262144
SYNC_RESULTS_MAX = 400
SYNC_STATES_MAX = 100
SYNC_IDLE_HOURS = 48

SIGNAL_URL = "https://cdn.wfsim.app/work.json"

#: `docs/CLIENTS.md` §Values defaults.  The server may move any of these; a
#: sync reply's `dials` is authoritative and merged over this.
SYNC_DEFAULT_DIALS = {
    "pack8_ms": 600000,
    "pack8_max": 200,
    "sync_lease_ms": 1500000,
    "pack_lease_ms": 300000,
    "sync_low_ms": 120000,
    "sync_floor_ms": 60000,
    "sync_idle_ms": 20000,
    "flush_ms": 600000,
    "packs_held": 2,
    "row_ceiling_ms": 7200000,
    "poll_ms": 10000,
    "jitter_ms": 5000,
    "urgent_jitter_ms": 2000,
    "report_ms": 3600000,
}


class SyncDials:
    """Authoritative dials: doc defaults merged with each sync reply."""

    def __init__(self) -> None:
        self.d = dict(SYNC_DEFAULT_DIALS)

    def merge(self, reply_dials) -> None:
        if not isinstance(reply_dials, dict):
            return
        for key, default in SYNC_DEFAULT_DIALS.items():
            value = reply_dials.get(key)
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                self.d[key] = number

    def get(self, key: str) -> int:
        try:
            return int(self.d.get(key, SYNC_DEFAULT_DIALS[key]))
        except (TypeError, ValueError, KeyError):
            return SYNC_DEFAULT_DIALS.get(key, 0)


def clamp_lanes(lanes: int) -> int:
    try:
        return max(1, min(256, int(lanes)))
    except (TypeError, ValueError):
        return 1


def clamp_pace_ms(pace_ms: int, dials: SyncDials) -> int:
    try:
        pace = int(pace_ms)
    except (TypeError, ValueError):
        pace = 0
    if pace < 100:
        pace = 100
    return min(pace, dials.get("sync_lease_ms"))


#: THE PAGE'S OWN RETRY LADDER (69-board-work.js `SEND_AGAIN_MS`), in seconds.
#: A short pack's lease is `pack_lease_ms` (5 min) and a sync that cannot
#: renew cannot keep it: the old 30·2^n ladder topped out at five minutes, so
#: one slow backoff let the rows lapse before the next sync.  3/10/30 then 30
#: keeps every lease renewable without hammering a dead endpoint.
SYNC_RETRY_LADDER_S = (3.0, 10.0, 30.0)


def sync_backoff_s(fails: int) -> float:
    """Seconds until the next sync after `fails` consecutive transport errors."""
    try:
        n = int(fails)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        return 0.0
    return SYNC_RETRY_LADDER_S[min(n - 1, len(SYNC_RETRY_LADDER_S) - 1)]


def compute_want_ms(pending_board_rows: int, pace_ms: int, dials: SyncDials,
                    has_room: bool) -> int:
    """How much work to ask for: a full pack, or nothing but a renew.

    Mirrors the protocol-7 prefetch gate: ask only with a free slot.  (The old
    `pool_full` riven cap is gone — the client either refuses rivens outright
    or computes them; a pool of rivens pending is never a reason to starve.)
    `pending * pace < sync_low_ms` is the protocol-8 form of the reference
    page's 15 s refill rule.
    """
    pack8_ms = dials.get("pack8_ms")
    if not has_room:
        return 0
    if pending_board_rows <= 0:
        return pack8_ms
    try:
        pace = max(100, int(pace_ms))
    except (TypeError, ValueError):
        pace = 100
    if pending_board_rows * pace < dials.get("sync_low_ms"):
        return pack8_ms
    return 0


def build_sync_body(*, verifier: str, engine: str, consent_v: int,
                    consent_at: str, lanes: int, pace_ms: int, want_ms: int,
                    held: list, results: list, keep=None,
                    refused: list | None = None, idle: list | None = None,
                    states: list | None = None,
                    since: str | None = None) -> dict:
    """Assemble one `/api/board/sync` request, oldest results first.

    `results` beyond the caps are left for the next sync (oldest first, so
    the longest-waiting answers always win the slot); the whole body is kept
    under `sync_body`.
    """
    body: dict = {
        "verifier": verifier, "engine": engine, "protocol": SYNC_PROTOCOL,
        "consent": {"v": int(consent_v), "at": consent_at},
        "lanes": clamp_lanes(lanes), "pace_ms": max(100, int(pace_ms or 100)),
        "want_ms": max(0, min(int(want_ms or 0), SYNC_DEFAULT_DIALS["pack8_ms"])),
        "held": [{"lease": h["lease"], "rows": list(h["rows"])} for h in held],
        "results": list(results[:SYNC_RESULTS_MAX]),
    }
    if keep:
        body["keep"] = keep
    if refused:
        body["refused"] = list(refused)
    if idle:
        body["idle"] = list(idle)
    if states:
        body["states"] = list(states[:SYNC_STATES_MAX])
    if since:
        body["since"] = since
    # Oldest results first already; shed newest syncs' worth until it fits.
    while len(body["results"]) > 1:
        try:
            size = len(json.dumps(body, ensure_ascii=False))
        except Exception:
            break
        if size <= SYNC_BODY_MAX:
            break
        body["results"].pop()
    return body


# ------------------------------------------------------------ idle accounting
def utc_hour(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


def idle_tick(store: dict, vid: str, hour: str, asks: int = 0,
              failed: int = 0, lanes: int = 0) -> None:
    bucket = store.setdefault(vid, {})
    row = bucket.setdefault(hour, {"asks": 0, "failed": 0, "lanes": 0})
    row["asks"] += int(asks)
    row["failed"] += int(failed)
    row["lanes"] += int(lanes)
    cutoff = utc_hour(time.time() - SYNC_IDLE_HOURS * 3600)
    for old in [h for h in bucket if h < cutoff]:
        del bucket[old]


def idle_payload(store: dict, vid: str) -> list:
    bucket = store.get(vid) or {}
    return [{"hour": h, "asks": r["asks"], "failed": r["failed"],
             "lanes": r["lanes"]} for h, r in sorted(bucket.items())]


def idle_clear(store: dict, vid: str) -> None:
    store.pop(vid, None)


# ------------------------------------------------------------ the work signal
def fetch_work_signal(timeout: int = 15) -> dict | None:
    """One `GET work.json`; None on any failure (caller falls back)."""
    try:
        req = urllib.request.Request(SIGNAL_URL,
                                     headers={"User-Agent": "wfsim-client"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode() or "{}")
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("v") != 1:
        return None
    return body


def signal_fallback(body: dict | None, now: float) -> bool:
    """Unreachable, older than 90 s, or `mode: "ask"` → sync directly."""
    if not isinstance(body, dict):
        return True
    if body.get("mode") == "ask":
        return True
    try:
        import datetime
        at = datetime.datetime.fromisoformat(
            str(body.get("at") or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return True
    return (now - at) > 90.0


def signal_wait_bounds(body: dict, dials: SyncDials) -> tuple[float, float]:
    """(lo, hi) seconds before an idle client syncs; caller rolls the dice."""
    try:
        urgent = int(body.get("urgent") or 0)
    except (TypeError, ValueError):
        urgent = 0
    try:
        opened = int(body.get("open") or 0)
    except (TypeError, ValueError):
        opened = 0
    if urgent > 0:
        span = dials.get("urgent_jitter_ms") / 1000.0
        return 0.0, max(span, 0.0)
    if opened > 0:
        span = dials.get("jitter_ms") / 1000.0
        return 0.0, max(span, 0.0)
    return -1.0, -1.0


def roll_wait(bounds: tuple[float, float]) -> float | None:
    if bounds[0] < 0:
        return None
    return random.uniform(bounds[0], bounds[1])


# ------------------------------------------------------------ negotiation
def offered_protocols(*sources) -> list:
    """Union of `protocols_served`/`protocols` seen, as ints."""
    out: list = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in ("protocols_served", "protocols"):
            for value in src.get(key) or []:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number not in out:
                    out.append(number)
    return out


#: THE BOARD WIRE.  Protocol 8 is the only one this client asks for: one
#: door, one writer, no in-app switch.  The single exception is the server's
#: own `fallback: 7` reply (worker/sync.js, the owner's switch), which
#: docs/CLIENTS.md requires every client to honour for the rest of the
#: session — `fleet._board_pin7` carries that, and it is the only thing that
#: can put this client back on the classic `/work` door.
BOARD_PROTOCOL = "8"


def resolve_board_protocol(cfg_mode: str = "", served: list | None = None,
                           pinned7: bool = False) -> str:
    """Which board wire to speak: "8", or "7" under a server-forced fallback."""
    if pinned7:
        return "7"
    return BOARD_PROTOCOL
