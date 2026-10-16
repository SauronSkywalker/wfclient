# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""The two HTTP surfaces: the WFSim server, and a local Rust engine process.

Nothing here knows about state machines or the UI — it is the wire.
"""
from __future__ import annotations
import json, time, urllib.error, urllib.request

import cores
from work_model import PROTOCOL, CONSENT_V

BASE = "https://wfsim.app"
UA = "wfsimpp-client/0.1"
#: These endpoints carry an accumulator large enough that logging the whole
#: body doubles JSON serialization and dominates the on-disk log.  Summary
#: mode records the endpoint/timing and omits the body.
LARGE_LOG_ENDPOINTS = {"/api/board/fold", "/api/board/score",
                        "/api/optimize", "/api/optimize/status"}


class Remote:
    """wfsim.app. `cookie` is only needed to claim a verifier."""

    def __init__(self, timeout: int = 120, logger=None):
        self.timeout = timeout
        self.cookie: str | None = None
        self.log = logger

    def _log_request(self, method: str, path: str, body=None,
                     include_body: bool = False) -> None:
        if not self.log:
            return
        if include_body:
            self.log.event("server_request", method=method, path=path, body=body)
            return
        self.log.event("server_request", method=method, path=path)

    def _log_response(self, method: str, path: str, status, body,
                      t0: float) -> None:
        if self.log:
            self.log.event("server_response", method=method, path=path,
                           status=status, body=body,
                           ms=int((time.time() - t0) * 1000))

    def _log_http_error(self, method: str, path: str, code, raw: str,
                        t0: float) -> None:
        if self.log:
            self.log.event("server_http_error", method=method, path=path,
                           status=code, body=raw,
                           ms=int((time.time() - t0) * 1000))

    def _log_transport_error(self, method: str, path: str, error: str,
                             t0: float) -> None:
        if self.log:
            self.log.event("server_transport_error", method=method, path=path,
                           error=error, ms=int((time.time() - t0) * 1000))

    @staticmethod
    def _get_log_body(out, log_body: bool):
        if log_body:
            return out
        return {"omitted": True,
                "keys": sorted(out.keys()) if isinstance(out, dict) else None,
                "contributors": (len(out.get("contributors") or [])
                                 if isinstance(out, dict) else None)}

    def post(self, path: str, body: dict, cookie: str | None = None,
             timeout: int | None = None) -> dict:
        self._log_request("POST", path, body, include_body=True)
        t0 = time.time()
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                out = json.loads(r.read().decode() or "{}")
                sc = r.headers.get("set-cookie")
                if sc and "wfsim_session=" in sc:
                    self.cookie = sc.split(";")[0]
                self._log_response("POST", path, getattr(r, "status", 200), out, t0)
                return out
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:4000]
            self._log_http_error("POST", path, e.code, raw, t0)
            return {"http_error": e.code, "body": raw[:200]}
        except Exception as e:
            self._log_transport_error("POST", path, f"{type(e).__name__}: {e}", t0)
            return {"transport_error": str(e)}

    def get(self, path: str, cookie: str | None = None,
            log_body: bool = True) -> dict:
        self._log_request("GET", path)
        t0 = time.time()
        req = urllib.request.Request(BASE + path, headers={"User-Agent": UA})
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode() or "{}")
                self._log_response("GET", path, getattr(r, "status", 200),
                                   self._get_log_body(out, log_body), t0)
                return out
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:4000]
            self._log_http_error("GET", path, e.code, raw, t0)
            return {"http_error": e.code, "body": raw[:200]}
        except Exception as e:
            self._log_transport_error("GET", path, f"{type(e).__name__}: {e}", t0)
            return {"transport_error": str(e)}

    # ---- the API's own vocabulary ----
    def login(self, email: str, password: str) -> dict:
        return self.post("/api/auth/email/login", {"email": email, "password": password})

    def claim(self, verifier: str, label: str) -> dict:
        return self.post("/api/account/devices/claim", {"verifier": verifier, "label": label}, self.cookie)

    def release_info(self) -> dict:
        return self.get("/release.json")

    def work(self, verifier: str, engine: str, lanes: int, consent_at: str,
             ahead: bool = False, pace_ms: int | None = None,
             timeout: int = 30) -> dict:
        """ONE work item under protocol 7.

        A board ask returns a `pack` of rows; a riven ask returns the single
        `work` shape.  `ahead:true` asks for the next work while the current
        lease is still held.  `pace_ms` is the observed board-row wall time and
        is what the server uses to size a pack.
        """
        body = {
            "verifier": verifier, "engine": engine, "protocol": PROTOCOL,
            "consent": {"v": CONSENT_V, "at": consent_at}, "lanes": lanes,
        }
        if ahead:
            body["ahead"] = True
        if pace_ms is not None:
            body["pace_ms"] = max(100, int(pace_ms))
        return self.post("/api/board/work", body, timeout=timeout)

    def board_sync(self, body: dict, timeout: int = 30) -> dict:
        """ONE protocol-8 sync: held rows, results, refusals, queries in.

        The reply is always `200` for a well-formed body (an unknown id gets
        `403`); transport/HTTP failures surface as `transport_error` /
        `http_error` like every other door.
        """
        return self.post("/api/board/sync", body, timeout=timeout)

    def answers(self, lease: str, verifier: str, engine: str,
                answers: list[dict], done: bool = False) -> dict:
        """The protocol-7 board result upload, one pack lease at a time."""
        return self.post("/api/board/answers", {
            "lease": lease, "verifier": verifier, "engine": engine,
            "answers": answers, "done": bool(done)})

    def verify(self, lease: str, verifier: str, engine: str,
               score: float, metric: str, work: int, compute_ms: int) -> dict:
        """Legacy v6 endpoint; kept only for old replay/debug callers."""
        return self.post("/api/board/verify", {
            "lease": lease, "verifier": verifier, "engine": engine,
            "score": score, "metric": metric, "work": work, "compute_ms": compute_ms})

    def release_lease(self, lease: str, verifier: str,
                      timeout: int | None = None) -> dict:
        """Give a board pack back.  `timeout` matters on the way out: the
        default is two minutes, and a shutdown cannot spend that per verifier."""
        return self.post("/api/board/release",
                         {"lease": lease, "verifier": verifier},
                         timeout=timeout)

    def board_renew(self, lease: str, verifier: str, question: str) -> dict:
        """HEARTBEAT for one board row: holds it another `renew_ms`.

        A pack's lease is no longer a deadline.  A page still computing a row
        renews it and is never cut off; only a silent one loses it.  Returns
        `{ok, held, renew_ms}` — `held:false` means another machine has the row.
        """
        return self.post("/api/board/renew",
                         {"lease": lease, "verifier": verifier,
                          "question": question})

    def appraise_renew(self, code: str, lease: str, verifier: str) -> dict:
        """HEARTBEAT for a riven lease.  No `release` key — that is the
        give-back, and sending it here would throw the work away."""
        return self.post(f"/api/appraise/{code}/renew",
                         {"lease": lease, "verifier": verifier})

    def appraise_result(self, code: str, body: dict) -> dict:
        return self.post(f"/api/appraise/{code}/result", body)

    def appraise_release(self, code: str, lease: str, verifier: str,
                         timeout: int | None = None) -> dict:
        return self.post(f"/api/appraise/{code}/renew",
                         {"lease": lease, "verifier": verifier, "release": True},
                         timeout=timeout)

    def board_mine(self, verifier: str, keys: list[dict],
                   timeout: int = 15) -> dict:
        return self.post("/api/board/mine",
                         {"verifier": verifier, "orders": keys}, timeout=timeout)

    def appraise_mine(self, verifier: str, tasks: list[dict],
                      timeout: int = 15) -> dict:
        return self.post("/api/appraise/mine",
                         {"verifier": verifier, "tasks": tasks}, timeout=timeout)

    def demand(self) -> dict:
        return self.get("/api/board/demand")

    def points(self, verifier: str, since: str | None = None) -> dict:
        """WHAT THIS DEVICE HAS EARNED, as the server counts it. `points` is
        work/1e9 — one point per thousand million of `Shard::work` — and it only
        counts a task the server has itself seen agreed (`worker/contribution.js`)."""
        body = {"verifier": verifier}
        if since:
            body["since"] = since
        return self.post("/api/board/points", body)

    def board_ledger(self, verifier: str, from_hour: str = "") -> dict:
        """The full verifier's ledger, including the holder labels in hours."""
        body = {"verifier": verifier}
        if from_hour:
            body["from"] = from_hour
        return self.post("/api/board/ledger", body, self.cookie)

    def account_info(self) -> dict:
        """The signed-in account itself, used as the light login probe."""
        return self.get("/api/account", self.cookie)

    def account_devices(self) -> dict:
        """THE OWNER'S DEVICE LIST — needs the session cookie. It is also the
        only place the account's own total is named."""
        return self.get("/api/account/devices", self.cookie)

    def contributors(self, limit: int = 100) -> dict:
        """The public all-time contributor ranking.

        The official page asks this every 60 seconds; `limit` is only how many
        rows we keep, never a polling interval.
        """
        n = max(1, min(2000, int(limit)))
        return self.get(f"/api/contributors?period=all&limit={n}",
                        self.cookie, log_body=False)


class RowRefused(RuntimeError):
    """THE ENGINE REFUSED THIS ROW — not this machine.

    `/api/board/order` validates the build before any fight: a melee row with
    no stance in its 8 main slots is the one that happens
    (`engine/src/board/builds/validate.rs`, `STANCE_REQUIRED`).  The refusal
    says nothing about the pack, so the caller drops the row and keeps the
    rest — which is what the reference client does
    (`web/src/static/app/69-board-work.js` `packRowOnce`: a row the engine
    refused "is left to the pack's end").
    """
    def __init__(self, message: str, *, endpoint: str = ""):
        super().__init__(message)
        self.endpoint = endpoint


class Engine:
    """One local Rust engine process."""

    def __init__(self, exe: str, port: int, cpu_limit_percent: int = 100, logger=None):
        self.exe = exe
        self.port = port
        self.proc = None
        self.log = logger
        self.log_tag = None
        try:
            self.cpu_limit_percent = max(10, min(100, int(cpu_limit_percent)))
        except Exception:
            self.cpu_limit_percent = 100
        #: normally the Fleet's SHARED job object.  One job for all three
        #: engines is what makes 50% mean 50% total, not 50% each.
        self.cpu_limit = None
        self.owns_cpu_limit = False
        self.cpu_limit_error = ""
        #: open stderr log handle while the engine runs; see `_open_stderr`.
        self._err = None

    def set_cpu_percent(self, percent: int) -> bool:
        """Apply/update the hard cap immediately on this running engine."""
        try:
            self.cpu_limit_percent = max(10, min(100, int(percent)))
        except Exception:
            self.cpu_limit_percent = 100
        if self.cpu_limit is None:
            self.cpu_limit = cores.CpuRateLimit(self.cpu_limit_percent)
            self.owns_cpu_limit = True
        if self.proc is None:
            return True
        ok = self.cpu_limit.set_percent(self.cpu_limit_percent)
        if ok and self.proc.pid not in self.cpu_limit.assigned_pids:
            ok = self.cpu_limit.assign(self.proc.pid)
        self.cpu_limit_error = "" if ok else (self.cpu_limit.error or "CPU limit unavailable")
        return ok

    def _stderr_path(self):
        """logs/engine-<port>.log beside the engine binary.

        The engine's Rust panics used to go to DEVNULL, so eight 0xc0000409
        deaths left zero evidence.  stderr now lands here (rotated at ~1 MB),
        and the boot failure message carries its tail.
        """
        import pathlib
        try:
            root = pathlib.Path(self.exe).resolve().parent.parent
            logs = root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            return logs / f"engine-{self.port}.log"
        except Exception:
            return None

    def _open_stderr(self):
        path = self._stderr_path()
        if path is None:
            return subprocess.DEVNULL, None
        try:
            # Rotate: a crash-looping engine must not grow without bound.
            if path.exists() and path.stat().st_size > 1_000_000:
                path.write_text("", encoding="utf-8")
            fh = open(path, "a", encoding="utf-8", errors="replace")
            fh.write(f"\n==== engine :{self.port} start "
                     f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ====\n")
            fh.flush()
            return fh, path
        except Exception:
            return subprocess.DEVNULL, None

    def start(self, cpu_limiter=None):
        import os, subprocess
        if cpu_limiter is not None:
            self.cpu_limit = cpu_limiter
            self.owns_cpu_limit = False
        env = dict(os.environ, WFSIM_PORT=str(self.port))
        err, err_path = self._open_stderr()
        self._err = err if hasattr(err, "write") else None
        self.proc = subprocess.Popen([self.exe], env=env,
                                     stdout=subprocess.DEVNULL, stderr=err)
        # BELOW NORMAL: an engine thread only runs when the client (one class
        # above normal) is not using the CPU — the client's loop, TLS and
        # logger can no longer queue behind compute work.
        cores.set_priority_class(self.proc.pid, cores.BELOW_NORMAL_PRIORITY_CLASS)
        # Attach the job before the engine can be asked to compute.  A 100%
        # cap is still attached so later TUI changes take effect immediately.
        self.set_cpu_percent(self.cpu_limit_percent)
        if self.log:
            self.log.event("engine_start", port=self.port, pid=self.proc.pid,
                           priority="below_normal", stderr=str(err_path or ""))
        for _ in range(120):
            try:
                self.get("/api/meta", timeout=5); return
            except Exception:
                time.sleep(0.25)
        tail = ""
        try:
            if err_path and err_path.exists():
                tail = err_path.read_text(encoding="utf-8",
                                          errors="replace")[-1500:]
        except Exception:
            pass
        self._close_stderr()
        raise RuntimeError(f"engine on :{self.port} did not come up"
                           + (f"\n--- stderr tail ---\n{tail}" if tail else ""))

    def _close_stderr(self) -> None:
        fh = getattr(self, "_err", None)
        self._err = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try: self.proc.wait(timeout=15)
            except Exception: self.proc.kill()
            self.proc = None
        self._close_stderr()
        if self.owns_cpu_limit and self.cpu_limit is not None:
            self.cpu_limit.close()
        self.cpu_limit = None
        self.owns_cpu_limit = False

    def _engine_log_body(self, path: str, data):
        if path in LARGE_LOG_ENDPOINTS and not getattr(self.log, "full", False):
            return {"omitted": True,
                    "keys": sorted(data.keys()) if isinstance(data, dict) else None}
        return data

    def post(self, path: str, body: dict, timeout: int = 3600, log: bool = True) -> dict:
        if log and self.log:
            self.log.event("engine_request", port=self.port, endpoint=path,
                           tag=self.log_tag, body=self._engine_log_body(path, body))
        t0 = time.time()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode() or "{}")
                if log and self.log:
                    self.log.event("engine_response", port=self.port, endpoint=path,
                                   tag=self.log_tag, body=self._engine_log_body(path, out),
                                   ms=int((time.time() - t0) * 1000))
                return out
        except Exception as e:
            if log and self.log:
                self.log.event("engine_error", port=self.port, endpoint=path,
                               tag=self.log_tag, error=f"{type(e).__name__}: {e}",
                               ms=int((time.time() - t0) * 1000))
            raise

    def get(self, path: str, timeout: int = 60) -> dict:
        if self.log:
            self.log.event("engine_request", port=self.port, endpoint=path,
                           tag=self.log_tag, body=None)
        t0 = time.time()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as r:
                out = json.loads(r.read().decode() or "{}")
                if self.log:
                    body_out = out
                    if path == "/api/meta" and isinstance(out, dict):
                        body_out = {"keys": sorted(out.keys()),
                                    "bytes": len(json.dumps(out, default=str))}
                    self.log.event("engine_response", port=self.port, endpoint=path,
                                   tag=self.log_tag, body=body_out,
                                   ms=int((time.time() - t0) * 1000))
                return out
        except Exception as e:
            if self.log:
                self.log.event("engine_error", port=self.port, endpoint=path,
                               tag=self.log_tag, error=f"{type(e).__name__}: {e}",
                               ms=int((time.time() - t0) * 1000))
            raise

    def set_cpu_sets(self, ids) -> bool:
        """Default CPU Sets for this engine.

        The engine spawns its workers with `std::thread::scope` per batch, so
        a default set applied now is inherited by every thread it spawns
        afterwards — no restart, and nothing about the arithmetic changes.
        """
        if self.proc is None:
            return False
        return cores.set_process_cpu_sets(self.proc.pid, ids)

    def pin_affinity(self, cpus) -> bool:
        """Restrict this engine to `cpus` (logical processor numbers).

        An empty list means the whole machine; the caller re-applies the
        slot's CPU Sets afterwards.  Used only around a ONE-LANE call, where
        there is no parallelism to lose and the point is the fastest core.
        """
        if self.proc is None:
            return False
        return cores.pin(self.proc.pid, cpus)

    def set_power_throttling(self, slow: bool) -> bool:
        """EcoQoS: `e` asks Windows to treat the engine as a background app."""
        if self.proc is None:
            return False
        return cores.set_power_throttling(self.proc.pid, slow)

    def set_optimize_threads(self, threads: int) -> dict:
        """Change the riven optimizer's worker budget for its next batch."""
        return self.post("/api/optimize/threads", {"threads": max(1, int(threads))},
                         timeout=5, log=False)

    # ---- one board row, the scorer's own path ----
    def board_row(self, record: dict, ruler: str, mode: str, lanes: int, log_tag=None):
        self.log_tag = log_tag
        """ONE ROW, THE SCORER'S OWN PATH, IN ONE CALL.

        The engine orders, fights, folds and scores the row itself
        (`core-host` `/api/board/row`) — the same `wfsim_webapi` functions the
        four calls used to run from here — so a row is one round trip and the
        megabyte of shards the fold carries never leaves the engine process.

        THERE IS NO FALLBACK.  An engine that does not answer this door is an
        engine to rebuild, not one to work around: a second implementation of
        the same row would be a second place for the two to drift apart.  The
        settings screen's 快速重新编译引擎 button is how it is brought forward.

        A refusal says WHICH STEP made it: `order` is the row's own validation
        (`validate_for_board`: a build no fight can run) and the caller skips
        that row; anything else is this process failing, and the row is kept.
        """
        try:
            one = self.post("/api/board/row",
                            {"record": record, "ruler": ruler, "mode": mode,
                             "threads": max(1, int(lanes))})
        except urllib.error.HTTPError as e:
            # The engine speaks HTTP, so "no such door" is an exception, not a
            # `{http_error: 404}` body.  Catch it HERE rather than decoding
            # every response: a 404 means the core on disk predates the row
            # door and the pack must not go around it.
            if e.code == 404:
                raise RuntimeError(
                    "引擎没有 /api/board/row：这是旧核心。请在设置里点"
                    "【快速重新编译引擎】或重启客户端更新后再跑") from None
            raise
        if isinstance(one, dict) and one.get("ok"):
            return {"score": one["score"], "metric": one["metric"],
                    "work": one["work"], "counts": one.get("counts") or {},
                    "request": one.get("request") or {}}
        # A transport-level 404 (engine unreachable path) may still surface as
        # a dict; keep the reachable branch for that shape.
        if isinstance(one, dict) and (one.get("http_error") == 404
                                      or one.get("error") == "not found"):
            raise RuntimeError(
                "引擎没有 /api/board/row：这是旧核心。请在设置里点"
                "【快速重新编译引擎】或重启客户端更新后再跑")
        if (one or {}).get("step") == "order":
            # THE ROW, not the machine: the board's own validation refused it.
            raise RowRefused(one.get("error") or "order refused",
                             endpoint="/api/board/order")
        raise RuntimeError((one or {}).get("error") or "board row refused")

    # ---- SEVERAL board rows in ONE call (2.2b) ----
    def board_rows_batch(self, rows, lanes: int, log_tag=None):
        """SEVERAL ROWS IN ONE ENGINE CALL, PIPELINED INSIDE THE ENGINE.

        `rows` is a list of `(record, ruler, mode)`.  The engine overlaps each
        row's serial `score` with the NEXT row's fold, so the lanes stop waiting
        through it: `PHASE` puts `score` at 6-20 ms on a 0.45-2.4 s row
        (1.2-4.3%), and 30 rows in batches of 8 measured **3.9% faster** than the
        same rows one call each, bit-identical (0/30 differences).

        SAME DOOR, SAME SEMANTICS, ONE PER ROW: the list is exactly as long as
        `rows`; a row the board's own validation refuses becomes a `RowRefused`
        for THAT row and a row the engine failed on becomes a RuntimeError for
        that row, so the caller can skip or keep each one without losing the
        rest of the batch.  A 404 raises once, for the whole batch: an engine
        without this door is an engine to rebuild.
        """
        self.log_tag = log_tag
        body = {"threads": max(1, int(lanes)),
                "rows": [{"record": r, "ruler": ru, "mode": m}
                         for (r, ru, m) in rows]}
        try:
            got = self.post("/api/board/rows", body)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(
                    "引擎没有 /api/board/rows：这是旧核心。请在设置里点"
                    "【快速重新编译引擎】或重启客户端更新后再跑") from None
            raise
        results = (got or {}).get("results") if isinstance(got, dict) else None
        if not isinstance(results, list) or len(results) != len(rows):
            raise RuntimeError("board rows refused: %s"
                               % ((got or {}).get("error") or "bad shape"))
        out = []
        for one in results:
            if isinstance(one, dict) and one.get("ok"):
                out.append({"score": one["score"], "metric": one["metric"],
                            "work": one["work"], "counts": one.get("counts") or {},
                            "request": one.get("request") or {}})
            elif (one or {}).get("step") == "order":
                out.append(RowRefused((one or {}).get("error") or "order refused",
                                      endpoint="/api/board/order"))
            else:
                out.append(RuntimeError((one or {}).get("error")
                                        or "board row refused"))
        return out

    # ---- one riven gain: one native job, polled while it runs ----
    def _start_optimize(self, request: dict, lanes: int):
        body = {**request, "threads": max(1, int(lanes))}
        started = self.post("/api/optimize", body, timeout=120)
        if not isinstance(started, dict) or started.get("ok") is False:
            raise RuntimeError((started or {}).get("error") or "optimize refused")
        if started.get("result") is not None:
            return None, started["result"]
        job_id = started.get("job_id")
        if job_id is None:
            raise RuntimeError("optimize returned neither a job id nor a result")
        return job_id, None

    def _poll_optimize(self, job_id, on_progress, poll: float) -> dict:
        last_status = time.time()
        while True:
            try:
                st = self.post("/api/optimize/status", {"id": job_id},
                               timeout=5, log=False)
            except Exception as e:
                # A local control-plane failure for a few seconds means the
                # engine is gone, not that the search took too long.
                if time.time() - last_status > 30.0:
                    raise RuntimeError(
                        f"engine status unavailable: {type(e).__name__}: {e}")
                time.sleep(max(0.05, float(poll)))
                continue
            last_status = time.time()
            if not isinstance(st, dict):
                continue
            if st.get("ok") is False:
                raise RuntimeError(st.get("error") or "optimize status refused")
            if on_progress:
                on_progress(st)
            if st.get("result") is not None:
                return st["result"]
            phase = st.get("phase")
            if phase in ("done", "cancelled", "error"):
                raise RuntimeError(
                    st.get("error") or f"optimize ended without a result ({phase})")
            time.sleep(max(0.02, min(float(poll), 0.25)))

    @staticmethod
    def _check_optimize_result(r) -> dict:
        if not isinstance(r, dict):
            raise RuntimeError("optimize returned a malformed result")
        if r.get("ok") is False:
            raise RuntimeError(r.get("error") or "optimize failed")
        if not r.get("work"):
            # A pre-work-stat core exposes only the final round.
            try:
                r["work"] = int(r.get("final_work") or 0)
            except (TypeError, ValueError):
                r["work"] = 0
        return r

    def riven_gain(self, request: dict, lanes: int, on_progress=None,
                   poll: float = 0.05, log_tag=None) -> dict:
        self.log_tag = log_tag
        """ONE NATIVE OPTIMIZE JOB, WITH A READ-ONLY STATUS WATCH.

        The native server now owns `/api/optimize` as a background job.  The
        first POST only registers it; `/api/optimize/status` carries progress
        and the final payload.  No quick-fleet HTTP loop is used: that protocol
        relies on worker-local state which cannot survive across native HTTP
        requests.  The job runs with its own thread budget and returns the
        official cumulative `work` field from the engine.
        """
        job_id, r = self._start_optimize(request, lanes)
        if r is None:
            r = self._poll_optimize(job_id, on_progress, poll)
        return self._check_optimize_result(r)

RIVEN_PREFIX = "riven:"          # 18-riven-mods.js
BOARD_RIVEN_SLOT = "riven"       # 87-headless.js


_ABSENT = object()  # what `undefined` is to JSON.stringify: a key that is dropped


def _js_array(value) -> list:
    """`(value || []).slice()`: an array, or nothing — never a string split apart."""
    return list(value) if isinstance(value, list) else []


def _js_or_ctx(res_value, ctx: dict, key: str):
    """`res[key] || ctx[key]`, an ABSENT ctx key staying absent.

    JS `a || b` yields `b` ITSELF — `""` included.  A fallback that returned
    `None` instead would write a `null` the page never writes, and the canon
    (worker/tasks.js `canon`) could never match another volunteer's answer.
    """
    if res_value:
        return res_value
    return ctx[key] if key in ctx else _ABSENT


def _board_mods(res: dict, ex, ctx: dict) -> list:
    mods = [BOARD_RIVEN_SLOT if str(m).startswith(RIVEN_PREFIX) else m
            for m in _js_array(res.get("mods")) if not (ex and m == ex)]
    stance = ctx.get("stance")
    if stance and stance not in mods:
        mods = mods + [stance]
    return mods


def _riven_id(res: dict) -> str:
    return next((m for m in _js_array(res.get("mods"))
                 if str(m).startswith(RIVEN_PREFIX)), "-")


def _riven_shape(ctx: dict, riven_id: str) -> dict:
    shape = (ctx.get("rivens") or {}).get(riven_id)
    return shape if shape else {"riven_pos": [], "riven_neg": ""}


def board_payload_from_result(res: dict, ctx: dict) -> dict:
    """`boardPayloadFromResult` (68-board-submit.js:215), transcribed.

    THE KEY SET IS THE CONTRACT.  A riven answer's fact is `canon({build, score,
    work})` compared as TEXT (worker/tasks.js), so one key the page would not
    have written — or a `null` where the page writes `""` — is a build that can
    never equal another volunteer's and is never credited (the `gone` state).
    `undefined` is an ABSENT property to `JSON.stringify`, which is why the
    frozen context's own keys decide whether `grip`/`loader`/`wielder` travel.

    The result names its mods; what it does NOT name — the ruler, the weapon,
    its mode, stance, valence and assembly, the hands it ran in — comes from the
    context the appraisal froze, and the riven's SHAPE comes from `ctx.rivens`,
    which is why a volunteer needs no local riven store.
    """
    ex = res.get("exilus")
    ex = ex if ex and ex != "none" else None
    riven_id = _riven_id(res)
    out: dict = {}
    if ctx.get("benchmark"):
        out["benchmark"] = ctx["benchmark"]
    if "weapon" in ctx:
        out["weapon"] = ctx["weapon"]
    mode = _js_or_ctx(res.get("mode"), ctx, "mode")
    if mode is not _ABSENT:
        out["mode"] = mode
    out["mods"] = _board_mods(res, ex, ctx)
    out.update(_riven_shape(ctx, riven_id))
    out["evolutions"] = _js_array(res.get("evolutions"))
    out["arcanes"] = [a for a in _js_array(res.get("arcane")) if a and a != "none"]
    valence = _js_or_ctx(res.get("valence"), ctx, "valence")
    if valence is not _ABSENT:
        out["valence"] = valence
    if ex:
        out["exilus"] = ex
    for key in ("grip", "loader", "wielder"):
        if key in ctx:
            out[key] = ctx[key]
    return out
