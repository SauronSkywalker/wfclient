"""Train a PGO-generate engine on a mixed board + riven workload, then dump.

Why not `run_corpus.py`: that runner kills the engine with `terminate()`, and a
killed process never reaches the profile runtime's exit hook on this target --
the counters come out as `truncated profile data` and `llvm-profdata merge`
refuses the file.  `POST /api/quit` exists for exactly that (core-host feature
`pgo-exit`): it calls `__llvm_profile_write_file()` before `exit(0)`.

Usage:
  python bench/pgo_train.py <instrumented exe> [--board 30] [--riven 1]
                            [--lanes 6] [--port 9450]

The engine is started with `cwd` = the profile directory named in the build's
`-Cprofile-generate=`, so both the file named by the runtime and any file that
falls back to the default name land where `llvm-profdata` will look.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from verify_batch import post, wait_port              # noqa: E402  (guarded)

PROF = pathlib.Path("C:/wfsim-build/profraw")


def spread(entries, want):
    if want <= 0:
        return []
    if want >= len(entries):
        return list(entries)
    step = len(entries) / want
    return [entries[int(i * step)] for i in range(want)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("--board", type=int, default=30)
    ap.add_argument("--riven", type=int, default=1)
    ap.add_argument("--lanes", type=int, default=6)
    ap.add_argument("--port", type=int, default=9450)
    ap.add_argument("--prof", default=str(PROF),
                    help="must match the -Cprofile-generate dir of the build")
    a = ap.parse_args()

    prof = pathlib.Path(a.prof)
    board = spread(json.load(open(BASE / "corpus.json", encoding="utf-8")),
                   a.board)
    riven = spread(json.load(open(BASE / "riven_corpus.json", encoding="utf-8")),
                   a.riven)
    prof.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, WFSIM_PORT=str(a.port),
               # Name the profile file ourselves.  -Cprofile-generate=<dir> bakes an
               # absolute path into the binary and the runtime creates it with a
               # narrow-char fopen: under a non-ASCII directory (D:\工程\...) the
               # create fails silently and the build then says "no .profraw".
               LLVM_PROFILE_FILE=str(prof / "default_%m_%p.profraw"))
    p = subprocess.Popen([a.engine], env=env, cwd=str(prof),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(a.port):
            print("engine never listened")
            return 1
        t0 = time.time()
        for it in board:
            post(a.port, dict(it["payload"], threads=it.get("lanes", a.lanes)))
        t_board = time.time() - t0
        print("board 训练 %d 条 %.1f 秒" % (len(board), t_board))

        t0 = time.time()
        for it in riven:
            body = dict(it["payload"]["request"], threads=it.get("lanes", a.lanes))
            started = post(a.port, body, path="/api/optimize", timeout=300)
            job = started.get("job_id") if isinstance(started, dict) else None
            res = started.get("result") if isinstance(started, dict) else None
            while res is None and job is not None and time.time() - t0 < 1800:
                time.sleep(0.5)
                st = post(a.port, {"id": job}, path="/api/optimize/status",
                          timeout=20)
                if isinstance(st, dict) and st.get("result") is not None:
                    res = st["result"]
                elif isinstance(st, dict) and (st.get("error")
                                               or st.get("ok") is False):
                    break
            if res is None:
                print("  riven %-14s 无结果" % it["id"])
        print("riven 训练 %d 条 %.1f 秒" % (len(riven), time.time() - t0))

        quit_ = post(a.port, {}, path="/api/quit", timeout=120)
        print("quit ->", str(quit_)[:120])
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print("!! did not exit after quit")
            p.terminate()
    finally:
        if p.poll() is None:
            p.terminate()

    files = sorted(prof.glob("*.profraw"))
    total = sum(f.stat().st_size for f in files)
    print("profraw %d 个，共 %d 字节" % (len(files), total))
    for f in files:
        print("   %-40s %d" % (f.name, f.stat().st_size))
    return 0 if total else 3


if __name__ == "__main__":
    sys.exit(main())
