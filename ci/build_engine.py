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

Editing this file does not rebuild anything by itself: only ``ci/patches/**`` and
``ci/core-host-template/**`` trigger the workflow on push, because an edit to a script
cannot move a byte of the engine.  A tooling change like this one rides along with the
next patch change, the six-hourly cron, or a manual "Run workflow" press -- one cold run
costs ~9 minutes of a Windows runner across four jobs (run 38991005621).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
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

# The MSVC toolset is DISCOVERED, not hardcoded: Visual Studio 2022 ships 14.4x
# and 2026 ships 14.5x, and whoever followed 自构建说明.md has whatever they just
# installed.  vswhere lists every install carrying the C++ workload; the newest
# toolset wins, and `--vs-toolset` / `WFSIM_VS_BIN` pin one on purpose.  Finding
# nothing is not an error: a CI runner already has link.exe on PATH.
VSWHERE = (pathlib.Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
           / "Microsoft Visual Studio" / "Installer" / "vswhere.exe")
VS_BUILD_TOOLS_2022 = (r"C:\Program Files (x86)\Microsoft Visual Studio\2022"
                       r"\BuildTools")
VS_BIN: pathlib.Path | None = None


def _toolsets(install: pathlib.Path) -> list[tuple[tuple[int, ...], str, pathlib.Path]]:
    """Every `VC/Tools/MSVC/<ver>/bin/Hostx64/x64` under one VS install."""
    root = install / "VC" / "Tools" / "MSVC"
    found = []
    if not root.is_dir():
        return found
    for d in root.iterdir():
        bin_dir = d / "bin" / "Hostx64" / "x64"
        if (bin_dir / "link.exe").exists():
            key = tuple(int(p) if p.isdigit() else 0 for p in d.name.split("."))
            found.append((key, d.name, bin_dir))
    return found


def find_vs_bin(prefer: str | None = None) -> pathlib.Path | None:
    """The newest installed MSVC toolset's x64 linker directory, or None."""
    pins = [("--vs-toolset", prefer)] if prefer else []
    env_pin = os.environ.get("WFSIM_VS_BIN")
    if env_pin:
        pins.append(("WFSIM_VS_BIN", env_pin))
    installs: list[pathlib.Path] = []
    if VSWHERE.exists():
        try:
            res = subprocess.run(
                [str(VSWHERE), "-all", "-prerelease", "-products", "*",
                 "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                 "-property", "installationPath"],
                capture_output=True, text=True, timeout=90)
            installs = [pathlib.Path(l.strip())
                        for l in res.stdout.splitlines() if l.strip()]
        except Exception as exc:                                  # noqa: BLE001
            print(f"note: vswhere failed ({exc}); leaving PATH alone")
    installs.append(pathlib.Path(VS_BUILD_TOOLS_2022))
    cands = [c for inst in installs for c in _toolsets(inst)]
    for flag, want in pins:
        hit = [c for c in cands if c[1] == want]
        if not hit:
            raise SystemExit(
                f"{flag}={want}: no such MSVC toolset (have: "
                + (", ".join(sorted(c[1] for c in cands)) or "none") + ")")
        cands = hit
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    _, ver, bin_dir = cands[-1]
    print(f"MSVC toolset {ver} -> {bin_dir}")
    return bin_dir


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
    if VS_BIN is not None:
        env["PATH"] = os.pathsep.join([str(VS_BIN), env.get("PATH", "")])
    run(["cargo", "build", "--profile", "maxperf", "--target", "x86_64-pc-windows-msvc",
         "-p", "wfsim-core", "--features", features], cwd=work, env=env)
    exe = out / "target" / "x86_64-pc-windows-msvc" / "maxperf" / "wfsim-core.exe"
    if not exe.exists():
        raise SystemExit(f"cargo reported success but {exe} is missing")
    print(f"built {exe} ({exe.stat().st_size:,} bytes)")
    return exe


def sha256_of(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def profile_key(root: pathlib.Path, work: pathlib.Path) -> str:
    """What a trained profile is strictly valid for.

    rustc treats a profile whose function control flow moved as a warning, so a
    near miss still pays off; the recorded key is the exact one anyway -- upstream
    commit, patches hash and the bytes of the training corpus.  A cached profile
    only ever saves time: the bit-identity gates below are what decide whether an
    engine gets published.
    """
    ident = {}
    try:
        ident = json.loads((root / "engine-src" / "ci-identity.json")
                           .read_text(encoding="utf-8"))
    except Exception:
        pass
    h = hashlib.sha256()
    for name in ("corpus.json", "riven_corpus.json", "riven3.json"):
        p = root / "ci" / "tools" / name
        if p.exists():
            h.update(p.read_bytes())
    return "|".join([str(ident.get("commit", "?")), str(ident.get("patches_sha", "?")),
                     h.hexdigest()[:16]])


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
    ap.add_argument("--gates", choices=("all", "board", "riven", "none"), default="all",
                    help="which bit-identity gates to run here.  CI runs them in their own "
                         "jobs (ci/gate_engine.py on the uploaded exe) so four runners work in "
                         "parallel and a failure is isolated; 'all' keeps the local one-shot run")
    ap.add_argument("--skip-gates", action="store_true")
    ap.add_argument("--reuse-profdata", action="store_true",
                    help="start from a cached training profile if one is present "
                         "(the cache restores it); trains from scratch if it is missing "
                         "or if the delivery build rejects it")
    ap.add_argument("--vs-toolset", default=None,
                    help="pin an MSVC toolset version, e.g. 14.44.35207; the default "
                         "is the newest installed (2022 ships 14.4x, 2026 ships 14.5x)")
    a = ap.parse_args()

    global VS_BIN
    VS_BIN = find_vs_bin(prefer=a.vs_toolset)

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
    profdata = pgo / "default.profdata"
    keyfile = pgo / "profile-key.json"
    key = profile_key(root, work)

    def train() -> None:
        """The expensive half: instrumented build, training run, merge (~6 min)."""
        # A retried run can find the previous run's profile here (the cache
        # restores it); merging stale counters with fresh ones would train on
        # the wrong mix.
        for old in pgo.glob("*.profraw"):
            old.unlink()
        if profdata.exists():
            profdata.unlink()

        # 1) instrumented build (only this one can write a profile: needs libprofiler_builtins)
        gen_exe = cargo_build(work, out, a.target_cpu, False, FEATURES_TRAIN,
                              [f"-Cprofile-generate={pgo}"])

        # Keep the instrumented binary aside under a name the final build cannot
        # overwrite.  Both passes share one CARGO_TARGET_DIR and one output path,
        # so training against `gen_exe` directly would silently produce no profile
        # the moment anything reorders these steps (measured: a profile-use exe at
        # that path answers normally and writes 0 bytes).
        #
        # Do NOT try to detect instrumentation by searching the exe for the symbol
        # name: the first version of this guard looked for `__llvm_profile_write_file`
        # and rejected a perfectly good instrumented binary on the runner (run
        # 38969851265 -- 21,674,496 bytes, and it trains fine).  The honest test is
        # the profile that either appears or does not, right below.
        trainer = pgo / "trainer.exe"
        shutil.copy2(gen_exe, trainer)

        # 2) train on the frozen corpus, then stop the engine through POST /api/quit so
        #    the profiler runtime flushes (a killed engine writes nothing).
        run([sys.executable, str(tools / "pgo_train.py"), str(trainer),
             "--board", str(a.board), "--riven", str(a.riven), "--prof", str(pgo),
             "--port", "9500"], cwd=tools)
        profraws = sorted(glob.glob(str(pgo / "*.profraw")))
        if not profraws:
            raise SystemExit(f"no .profraw produced in {pgo}")
        print(f"profraw files: {len(profraws)} "
              f"({sum(os.path.getsize(p) for p in profraws):,} bytes)")

        # 3) merge
        run([find_profdata(), "merge", "-o", str(profdata)] + profraws)
        print(f"profdata {profdata} ({profdata.stat().st_size:,} bytes)")
        keyfile.write_text(json.dumps(
            {"key": key, "profdata_bytes": profdata.stat().st_size,
             "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n",
            encoding="utf-8")

    def delivery_build():
        return cargo_build(work, out, a.target_cpu, a.crt_static, FEATURES_FINAL,
                           [f"-Cprofile-use={profdata}"])

    if a.reuse_profdata and profdata.exists():
        old = {}
        try:
            old = json.loads(keyfile.read_text(encoding="utf-8"))
        except Exception:
            pass
        print(f"reusing {profdata} ({profdata.stat().st_size:,} bytes, "
              f"trained for {str(old.get('key', 'unknown'))[:56]})")
        if old.get("key") != key:
            print(f"note: this source key is {key[:56]}; rustc only warns on a "
                  f"moved control flow, and the gates still check every bit")
    else:
        train()

    # 4) the delivery build itself.  A profile that no longer fits the source is
    #    only a warning for rustc, but if it really fails we train and rebuild
    #    instead of publishing nothing.
    try:
        exe = delivery_build()
    except subprocess.CalledProcessError:
        if not profdata.exists():
            raise
        print("note: the cached profile did not fit this source; training again")
        train()
        exe = delivery_build()

    # 5) bit-identity gates before anything is published.  In CI the gates run in
    #    their own jobs against the uploaded exe (ci/gate_engine.py), so board,
    #    anchors and the three rivens use four runners at once; this path stays for
    #    the local one-shot run and for --gates board/riven debugging.
    if a.skip_gates:
        a.gates = "none"
    if a.gates != "none":
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import gate_engine
        if a.gates in ("all", "board"):
            gate_engine.run_gate(exe, "board", port=9501, log_dir=out)
        if a.gates in ("all", "riven"):
            # >= 20 runs the full frozen corpus; a smaller number runs the 3-riven quick sample
            corp = root / "ci" / "tools" / ("riven_corpus.json" if a.rivens_gate >= 20
                                            else "riven3.json")
            gate_engine.run_gate(exe, "riven", riven_corpus=str(corp), port=9503, log_dir=out)

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
