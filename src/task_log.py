# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Complete per-task debug log for the portable client.

FOUR LEVELS, and the default is 关 (``"off"``): an untouched install writes
nothing.  ``"standard"`` (标准) keeps errors and the session's own start/stop,
``"verbose"`` (详细) adds the routine session/timing/upload records but omits
the large engine bodies, and ``"full"`` (完整) keeps every body plus the
CPU-scheduling records.  The level may be changed while the client runs.

When it is on, everything is written under client-portable/logs/:

  events-YYYYmmdd-HHMMSS.jsonl   one JSON object per event, append-only,
                                ONE FILE PER CLIENT START — a restart never
                                appends to the previous run's file
  tasks/<lease>.jsonl       append-only records for one lease

It is a debugging aid: computation and network behaviour are untouched.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path


class TaskLogger:
    """JSONL logger with FOUR levels, each a real strictness of its own.

    The level names are the page's (`app.py`): ``"off"`` (关) writes nothing at
    all — no directory, no file, no record, not even the redaction walk;
    ``"standard"`` (标准) keeps only errors; ``"verbose"`` (详细) keeps the
    medium set — session, timing and error data, with large engine bodies such
    as the board fold accumulator replaced by an omission marker — and
    ``"full"`` (完整) keeps every body and every scheduling record for
    debugging.

    ``off`` IS THE DEFAULT.  An untouched install leaves nothing on disk, and
    the early return in `event`/`task` is what makes that cost nothing.

    An older config may carry ``"summary"`` (the name this file used before the
    three-level split); it is read as ``"verbose"`` so nothing written by an
    earlier build loses its meaning.  A bare ``true``/``false`` from a build
    that only knew on/off reads as ``standard``/``off``.
    """
    #: The four levels, weakest first.  `off` is the production default.
    LEVELS = ("off", "standard", "verbose", "full")
    #: A value this build does not recognise means the same as the default:
    #: nothing written.  A typo must not silently start writing files.
    DEFAULT = "off"
    #: Spellings from the builds that had fewer levels, and from raw booleans.
    _ALIASES = {"summary": "verbose",
                "true": "standard", "on": "standard", "1": "standard",
                "false": "off", "none": "off", "0": "off", "": "off"}

    def __init__(self, root: str | Path, enabled=True):
        #: `enabled` carries the LEVEL as-is (a string) or a legacy boolean;
        #: wrapping it in `bool()` here once collapsed all levels to on/off.
        self.root = Path(root)
        if enabled is True:
            mode = "standard"
        elif enabled in (False, None):
            mode = "off"
        else:
            mode = str(enabled).strip().lower()
        mode = self._ALIASES.get(mode, mode)
        if mode not in self.LEVELS:
            mode = self.DEFAULT
        self.mode = mode
        self.enabled = mode != "off"
        #: 完整 keeps every body; 详细/标准 omit the large accumulators.
        self.full = mode == "full"
        #: 标准 keeps only errors.
        self.errors_only = mode == "standard"
        self.dir = self.root / "logs"
        self.tasks_dir = self.dir / "tasks"
        self._lock = threading.Lock()
        #: set once `logs/` exists — see `_ensure_ready`.
        self._ready = False
        #: ONE FILE PER START, named by the moment the client came up.  A
        #: per-day name buries a restart inside the previous run's file, so
        #: "what did THIS run do" needs a timestamp filter and a run that died
        #: early cannot be told from one that never started.  The format is
        #: unchanged JSONL and the `events-` prefix keeps the name recognizable.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"events-{stamp}.jsonl"
        self.events_path = self.dir / name
        if self.enabled:
            self._ensure_ready()
            self.event("session_start", pid=os.getpid(), mode=self.mode)

    def _ensure_ready(self) -> None:
        """CREATE `logs/` ON FIRST WRITE.  Call under `self._lock`.

        The level can be raised from 关 while the client runs, long after a
        start-up that skipped the mkdir — and appending to a directory that was
        never created would raise.  The events file name is settled here for
        the same reason: `one file per client start` still holds, but the name
        is only claimed at the moment the first record is actually written.
        """
        if self._ready:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        if self.events_path.exists():
            # Two clients in one folder, or a restart inside the same second:
            # the pid keeps the two files apart, not interleaved.
            self.events_path = self.dir / (
                f"{self.events_path.stem}-{os.getpid()}.jsonl")
        self._ready = True

    @staticmethod
    def _default(value):
        try:
            return str(value)
        except Exception:
            return repr(value)

    @staticmethod
    def _now() -> float:
        return time.time()

    #: Anything that may carry a credential, at any depth of a logged body.
    #: Matched on the LOWERCASED name with separators stripped, so camelCase
    #: (`privateKey`), kebab (`set-cookie`), prefixed (`wfsim_session_cookie`)
    #: and snake spellings all collapse onto the same fragment.
    SECRET_KEYS = frozenset({
        "password", "passwd", "cookie", "cookies", "authorization",
        "github_pat", "pat", "token", "secret", "api_key", "apikey",
        "access_token", "refresh_token", "private_key", "credential",
        "credentials", "set_cookie", "setcookie", "session", "sessionid",
        "auth", "authtoken", "apikeyheader", "privatekey", "accesskey",
    })

    @classmethod
    def _secret_key(cls, key) -> bool:
        name = "".join(ch for ch in str(key).lower() if ch.isalnum())
        if not name:
            return False
        return any(fragment in name for fragment in (
            "pass", "cookie", "auth", "secret", "token", "credential",
            "session", "apikey", "privatekey", "accesskey"))

    @classmethod
    def redact(cls, value):
        """Never write a password, session cookie or API credential.

        dicts and lists are walked by key/name; a secret found ANYWHERE inside
        a string (an error message, a URL query, a header dump) is masked in
        place, so a credential cannot leak through free text.
        """
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                out[k] = "***" if cls._secret_key(k) else cls.redact(v)
            return out
        if isinstance(value, (list, tuple)):
            return [cls.redact(x) for x in value]
        if isinstance(value, str):
            return cls._redact_text(value)
        return value

    #: `name=value` shapes that carry a credential.  The value may be quoted,
    #: base64, hex or JWT-ish ('.$'); anything up to a separator.  The NAME
    #: must be secret-ish, so ordinary text that merely contains "=" survives.
    _SECRET_IN_TEXT = re.compile(
        r"(?i)\b([a-z0-9_.\-]*(?:pass|cookie|auth|secret|token|credential|"
        r"session|apikey|apikey|privatekey|accesskey)[a-z0-9_.\-]*)"
        r"(\s*[:=]\s*)([\"']?)([^\s\"'&;,)\\]]{4,})\3")

    _SAFE_MARKER = "…redacted…"

    @classmethod
    def _redact_text(cls, text: str) -> str:
        if not text or len(text) < 4:
            return text
        return cls._SECRET_IN_TEXT.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}"
                     f"{cls._SAFE_MARKER}",
            text)

    #: 标准 (errors only) keeps an event whose kind says it is a failure.  The
    #: suffix covers the whole `*_error` family; the explicit set names the
    #: failure-shaped events that do not end that way.  `session_start` and
    #: `session_stop` are kept too — without them a 标准 file has no beginning
    #: or end and cannot be told from one that never ran.
    _ERROR_KINDS = frozenset({
        "session_start", "session_stop",
        "row_lost", "row_lost_after_reserve",
        "board_sync_stale", "board_sync_banned", "board_sync_malformed",
        "board_pack_stalled", "board_tail_failed", "board_kept_dropped",
        "riven_pending_dropped", "riven_pending_refused",
        "riven_answer_unconfirmed", "riven_pending_unconfirmed",
        "sync_pack_unparseable", "sync_riven_unparseable",
        "sync_pack_no_room", "sync_riven_no_room",
        "slot_watchdog_dead", "fetch_empty",
    })

    def _keep_event(self, kind: str) -> bool:
        if not self.errors_only:
            return True
        return kind.endswith("_error") or kind in self._ERROR_KINDS

    def event(self, kind: str, **data) -> None:
        #: 关 RETURNS HERE: the caller's data is never walked, redacted,
        #: serialized or opened, so "no logging" really costs nothing.
        if not self.enabled or not self._keep_event(kind):
            return
        rec = {"ts": self._now(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "kind": kind, **self.redact(data)}
        line = json.dumps(rec, ensure_ascii=False, default=self._default)
        with self._lock:
            self._ensure_ready()
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def task(self, lease_id: str | None, **data) -> None:
        """APPEND one task update, never rewrite the file.

        A per-lease JSONL file is the append-only form: one compact JSON
        object per update, in the order the events happened.  A large riven
        result no longer makes every later update read and serialize the whole
        previous file again.

        标准 keeps only updates that carry a failure — the task-side twin of
        `_keep_event`.
        """
        if not self.enabled or not lease_id:
            return
        if self.errors_only and not self._task_is_error(data):
            return
        lease_id = str(lease_id)
        rec = {"ts": self._now(),
               "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "lease": lease_id, **self.redact(data)}
        line = json.dumps(rec, ensure_ascii=False, default=self._default)
        with self._lock:
            self._ensure_ready()
            path = self.tasks_dir / f"{lease_id}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    @staticmethod
    def _task_is_error(data: dict) -> bool:
        """A task update worth keeping at 标准: one that names a failure."""
        if any(k in data for k in ("upload_exception", "renew_lost_row",
                                   "release_reason")):
            return True
        for key in ("upload_response", "kept_response", "fetch_response"):
            body = data.get(key)
            if isinstance(body, dict) and (
                    body.get("transport_error") or body.get("http_error")
                    or body.get("ok") is False):
                return True
        return False
