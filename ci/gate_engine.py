#!/usr/bin/env python3
"""Run one bit-identity gate slice against an already-built wfsim-core.exe.

The build job is not the gate job.  This script runs on a runner that never
compiles anything: it needs Python, the frozen corpora in `ci/tools/` and the exe
the build job uploaded.  Splitting the gates buys wall-clock time -- board (100
rows) and the wasm anchors (105 rows) take 63-135 s each, each riven 43-295 s, so
four runners finish the set in max(board+anchors, slowest riven) instead of the
sum (measured 256 s sequential on run 38970844958, 544 s on 38973837158).

    --gate board   100 frozen board rows + the 5 site-wasm anchors
    --gate riven   the riven slice; --riven-only picks exactly one entry

Every slice shares one verdict rule (`PASS` and `0 mismatches`/"0 条不一致") and
writes its full output to `<dist>/gate-<label>.log`, which the workflow uploads
so a failure is inspectable without re-running anything.
"""
import argparse
import os
import pathlib
import subprocess
import sys

TOOLS = pathlib.Path(__file__).resolve().parent / "tools"


def run_gate(exe: pathlib.Path, gate: str, *, riven_only: str = "",
             riven_corpus: str = "", port: int = 9501,
             log_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Run one slice and raise SystemExit if any frozen value moved.

    A gate that printed nothing, or that never reached a verdict, fails too:
    "0 mismatches" and "PASS" both have to be present.
    """
    exe = pathlib.Path(exe).resolve()
    if not exe.exists():
        raise SystemExit(f"no engine at {exe} (did the build job's artifact arrive?)")
    log_dir = pathlib.Path(log_dir) if log_dir else exe.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    if gate == "board":
        slices = [("board-100", [], port),
                  ("wasm-anchors-105", ["--anchors"], port + 1)]
    elif gate == "riven":
        # Resolve here: run_corpus.py runs with cwd=ci/tools, so a relative corpus
        # path given on the command line would be read from the wrong directory
        # (measured: "no ci/tools/riven3.json, skipping riven" + a hard failure).
        corpus = (pathlib.Path(riven_corpus).resolve() if riven_corpus
                  else (TOOLS / "riven_corpus.json"))
        extra = ["--corpus", str(TOOLS / "empty.json"), "--riven",
                 "--riven-corpus", str(corpus)]
        label = "riven-" + (riven_only or "all")
        if riven_only:
            extra += ["--riven-only", riven_only]
        slices = [(label, extra, port)]
    else:
        raise SystemExit(f"unknown --gate {gate!r} (board | riven)")

    for label, extra, p in slices:
        log = log_dir / f"gate-{label}.log"
        cmd = [sys.executable, str(TOOLS / "run_corpus.py"), str(exe),
               "--port", str(p)] + extra
        print(f"+ {label} gate: {' '.join(cmd)}", flush=True)
        # Pin both sides of the pipe to UTF-8: the runner's locale is cp1252 and a
        # mismatched decode turns the very log a human reads on failure into mojibake
        # (measured locally: `D:\工程\...` came back as `D:\宸ョ▼\...`).
        child_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.run(cmd, cwd=TOOLS, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=child_env)
        text = (proc.stdout or "") + (proc.stderr or "")
        log.write_text(text, encoding="utf-8")
        tail = "\n".join(text.strip().splitlines()[-6:])
        print(tail, flush=True)
        # The CI copy of the runner prints ASCII, the dev copy prints Chinese;
        # accept either so the same script works in both places.
        clean = ("0 mismatches" in text) or ("0 条不一致" in text)
        if proc.returncode != 0 or "PASS" not in text or not clean:
            raise SystemExit(f"{label} gate FAILED (see {log})")
        print(f"{label} gate OK ({log})", flush=True)
    return log_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="dist/wfsim-core.exe")
    ap.add_argument("--gate", required=True, choices=("board", "riven"))
    ap.add_argument("--riven-only", default="",
                    help="紫卡切片：id（如 riven-SR2N0246）或 1 起的序号")
    ap.add_argument("--riven-corpus", default="")
    ap.add_argument("--port", type=int, default=9501)
    ap.add_argument("--log-dir", default="")
    a = ap.parse_args()
    exe = pathlib.Path(a.exe)
    run_gate(exe, a.gate, riven_only=a.riven_only,
             riven_corpus=a.riven_corpus, port=a.port,
             log_dir=pathlib.Path(a.log_dir) if a.log_dir else exe.parent)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
