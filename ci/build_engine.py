"""CI step 2/2 - build the fast engine (MSVC + PGO + mimalloc) and gate it bit-for-bit.

    python ci/build_engine.py --work engine-src/work [--out dist] [--target-cpu x86-64-v3]

Why this shape (all numbers measured on the dev machine, 100 board rows, 6 lanes):

    bundled GNU toolchain + mimalloc     93.8 s   1.00x   <- what a user can self-build
    MSVC toolchain + mimalloc            44.0 s   2.13x
    MSVC + PGO + mimalloc                29.9 s   3.14x   <- what this workflow produces

So the two things a portable package cannot do itself are the MSVC target and the PGO
training pass; both need a machine with the Windows SDK / MSVC (a CI runner counts).

Gates (must pass, otherwise nothing is published):
    * 100 frozen board rows  - score/metric/work/counts bit-identical
    * +5 WASM anchors        - the site's own spot-check values, bit-identical

``--target-cpu native`` is deliberately NOT the default: a runner's CPU is not the user's,
so pin a portable baseline (x86-64-v3 == Haswell 2013+, measured 12.5% slower than native).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

TOOLCHAIN = "1.97.1-x86_64-pc-windows-msvc"
FEATURES_TRAIN = "wfsim-core/fast-alloc,wfsim-core/pgo-exit"
FEATURES_FINAL = "wfsim-core/fast-alloc"


def pgo_dir(out: pathlib.Path) -> pathlib.Path:
    """A profile directory the LLVM runtime can actually create.

    ``-Cprofile-generate=<dir>`` bakes the path into the binary, and the profile
    runtime creates the file with a narrow-char fopen.  Under a non-ASCII
    directory (a checkout below ``D:\\工程\\`` and the like) that create fails
    *silently*, the engine still answers and quits cleanly, and this script then
    stops with "no .profraw produced".  Measured: the very same instrumented
    binary writes a 2.9 MB profile the moment LLVM_PROFILE_FILE names an ASCII
    path.  So only the profile lives on an ASCII path; the build cache around it
    may stay wherever the caller asked for.
    """
    try:
        str(out).encode("ascii")
    except UnicodeEncodeError:
        fallback = pathlib.Path(tempfile.gettempdir()) / "wfsim-pgo"
        print(f"note: {out} is not ASCII; profiles go to {fallback}")
        return fallback
    return out / "pgo"


def run(cmd, cwd=None, env=None, check=True):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, check=check)


def find_profdata() -> str:
    pat = os.path.expanduser(
        f"~/.rustup/toolchains/{TOOLCHAIN}/lib/rustlib/*/bin/llvm-profdata.exe")
    hits = glob.glob(pat)
    if not hits:
        raise SystemExit("llvm-profdata.exe not found: rustup component add llvm-tools-preview")
    return hits[0]


def cargo_build(work: pathlib.Path, out: pathlib.Path, cpu: str, crt_static: bool,
                features: str, extra_flags: list[str]) -> pathlib.Path:
    flags = [f"-C target-cpu={cpu}"]
    if crt_static:
        flags.append("-C target-feature=+crt-static")
    flags += extra_flags
    (out / "tmp").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ,
               RUSTUP_TOOLCHAIN=TOOLCHAIN,
               CARGO_TARGET_DIR=str(out / "target"),
               TEMP=str(out / "tmp"), TMP=str(out / "tmp"),
               RUSTFLAGS=" ".join(flags))
    run(["cargo", "build", "--profile", "maxperf", "--target", "x86_64-pc-windows-msvc",
         "-p", "wfsim-core", "--features", features], cwd=work, env=env)
    exe = out / "target" / "x86_64-pc-windows-msvc" / "maxperf" / "wfsim-core.exe"
    if not exe.exists():
        raise SystemExit(f"cargo reported success but {exe} is missing")
    print(f"built {exe} ({exe.stat().st_size:,} bytes)")
    return exe


def gate(tools: pathlib.Path, exe: pathlib.Path, label: str, extra: list[str], port: int) -> None:
    log = tools.parent.parent / "dist" / f"gate-{label}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(tools / "run_corpus.py"), str(exe), "--port", str(port)] + extra
    print(f"+ {label} gate: {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, cwd=tools, capture_output=True, text=True)
    text = (p.stdout or "") + (p.stderr or "")
    log.write_text(text, encoding="utf-8")
    tail = "\n".join(text.strip().splitlines()[-4:])
    print(tail, flush=True)
    # The CI copy of the runner prints ASCII, the dev copy prints Chinese;
    # accept either so the same script works in both places.
    clean = ("0 mismatches" in text) or ("0 条不一致" in text)
    if p.returncode != 0 or "PASS" not in text or not clean:
        raise SystemExit(f"{label} gate FAILED (see {log})")


def sha256_of(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--work", default="engine-src/work")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--target-cpu", default="x86-64-v3")
    ap.add_argument("--crt-static", action="store_true",
                    help="link the CRT statically (no vcruntime140*.dll needed on the target)")
    ap.add_argument("--board", type=int, default=40, help="training board rows for PGO")
    ap.add_argument("--riven", type=int, default=1, help="training riven jobs for PGO")
    ap.add_argument("--rivens-gate", type=int, default=0,
                    help="0 = skip the (slow ~7 min) 20-riven gate, 3 = quick sample, 20 = full")
    ap.add_argument("--skip-gates", action="store_true")
    a = ap.parse_args()

    root = pathlib.Path(a.root).resolve()
    work = (root / a.work).resolve()
    out = (root / a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tools = root / "ci" / "tools"
    if not (work / "Cargo.toml").exists():
        raise SystemExit(f"{work} has no Cargo.toml - run ci/fetch_sources.py first")

    t0 = time.time()
    pgo = pgo_dir(out)
    pgo.mkdir(parents=True, exist_ok=True)
    # A retried run can find the previous run's profile here (the cache restores
    # it); merging stale counters with fresh ones would train on the wrong mix.
    for old in pgo.glob("*.profraw"):
        old.unlink()

    # 1) instrumented build (only this one can write a profile: needs libprofiler_builtins)
    gen_exe = cargo_build(work, out, a.target_cpu, False, FEATURES_TRAIN,
                          [f"-Cprofile-generate={pgo}"])

    # Keep the instrumented binary aside under a name the final build cannot
    # overwrite.  Both passes share one CARGO_TARGET_DIR and one output path, so
    # training against `gen_exe` directly would silently produce no profile the
    # moment anything reorders these steps (measured: a profile-use exe at that
    # path answers normally and writes 0 bytes).
    trainer = pgo / "trainer.exe"
    shutil.copy2(gen_exe, trainer)
    if b"__llvm_profile_write_file" not in trainer.read_bytes():
        raise SystemExit(f"{trainer} looks like a non-instrumented build")

    # 2) train on the frozen corpus, then stop the engine through POST /api/quit so the
    #    profiler runtime flushes (the engine is otherwise killed and writes nothing).
    run([sys.executable, str(tools / "pgo_train.py"), str(trainer),
         "--board", str(a.board), "--riven", str(a.riven), "--prof", str(pgo),
         "--port", "9500"], cwd=tools)
    profraws = sorted(glob.glob(str(pgo / "*.profraw")))
    if not profraws:
        raise SystemExit(f"no .profraw produced in {pgo}")
    print(f"profraw files: {len(profraws)} ({sum(os.path.getsize(p) for p in profraws):,} bytes)")

    # 3) merge
    profdata = pgo / "default.profdata"
    run([find_profdata(), "merge", "-o", str(profdata)] + profraws)
    print(f"profdata {profdata} ({profdata.stat().st_size:,} bytes)")

    # 4) the delivery build itself
    exe = cargo_build(work, out, a.target_cpu, a.crt_static, FEATURES_FINAL,
                      [f"-Cprofile-use={profdata}"])

    # 5) bit-identity gates before anything is published
    if not a.skip_gates:
        gate(tools, exe, "board-100", [], 9501)
        gate(tools, exe, "wasm-anchors-105", ["--anchors"], 9502)
        if a.rivens_gate:
            # >= 20 runs the full frozen corpus; a smaller number runs the 3-riven quick sample
            corp = root / "ci" / "tools" / ("riven_corpus.json" if a.rivens_gate >= 20
                                            else "riven3.json")
            gate(tools, exe, f"riven-{a.rivens_gate}",
                 ["--corpus", str(root / "ci" / "tools" / "empty.json"), "--riven",
                  "--riven-corpus", str(corp)], 9503)

    # 6) publish
    final = out / "wfsim-core.exe"
    shutil.copy2(exe, final)
    identity = {}
    id_path = work.parent / "ci-identity.json"
    if id_path.exists():
        identity = json.loads(id_path.read_text(encoding="utf-8"))
    identity.update({
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "built_on": os.environ.get("RUNNER_OS", os.name),
        "target_cpu": a.target_cpu,
        "crt_static": bool(a.crt_static),
        "features": FEATURES_FINAL,
        "pgo_trained_on": f"{a.board} board rows + {a.riven} riven jobs",
        "rustc": TOOLCHAIN,
        "sha256": sha256_of(final),
        "size": final.stat().st_size,
        "build_seconds": round(time.time() - t0, 1),
    })
    (out / "engine-identity.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
    print(json.dumps(identity, indent=2))
    print(f"DONE in {identity['build_seconds']}s -> {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
