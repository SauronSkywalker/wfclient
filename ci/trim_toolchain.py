"""Copy the part of a rustup toolchain we actually need, and nothing else.

A full ``1.97.1-x86_64-pc-windows-msvc`` toolchain is 1.1 GB, and 703 MB of that
is ``lib/rustlib/x86_64-pc-windows-msvc/bin``: LLVM utilities (``llc``,
``llvm-nm``, ``llvm-ar``, ``llvm-cov``, ...) that only matter when you are
building *for* that target with clang, plus a 93 MB ``wasm32`` std we never
touch.  What a cargo build of ``wfsim-core`` needs is:

* ``bin/rustc.exe``, ``bin/cargo.exe`` and the two big support dlls next to them
  (``rustc_driver-<hash>.dll`` 197 MB, ``std-<hash>.dll``);
* the target's own pre-compiled std: ``lib/rustlib/<target>/lib``;
* exactly one LLVM utility, ``llvm-profdata.exe`` (5 MB) — PGO merge;
* the licence files, because we redistribute this directory.

Everything else (``*.pdb`` 84 MB, clippy, rustdoc, rust-analyzer's proc-macro
server, the wasm32 target, the GNU target) is dropped.  Measured: 1.1 GB ->
346 MB, and a full MSVC + PGO build of the engine still succeeds from the
trimmed copy (see ``自构建说明.md`` 路径 C).

Usage::

    python ci/trim_toolchain.py --toolchain %USERPROFILE%/.rustup/toolchains/1.97.1-x86_64-pc-windows-msvc \
        --out dist/toolchain-msvc
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

TARGET = "x86_64-pc-windows-msvc"


def keep(rel: pathlib.PurePosixPath, name: str) -> bool:
    """Is this file worth shipping?  ``rel`` is the path inside the toolchain."""
    parts = rel.parts
    if not parts:
        return False
    top = parts[0]
    if top in ("etc",):
        return True
    if top == "share":
        # licences only: share/doc/rust/{COPYRIGHT*.html,README.md,licenses/**}
        return (len(parts) > 2 and parts[1] == "doc" and parts[2] == "rust"
                and (name.startswith("COPYRIGHT") or name in ("README.md", "LICENSE-APACHE",
                                                              "LICENSE-MIT", "LICENSE-THIRD-PARTY")
                     or (len(parts) > 3 and parts[3] == "licenses")))
    if top != "bin" and top != "lib":
        return False
    if top == "bin":
        # rustc, cargo, the dlls they load, and the tiny debugger shims.  clippy-driver,
        # rustdoc and rust-analyzer are executables of their own, so the suffix decides.
        return (name in ("rustc.exe", "cargo.exe") or name.endswith(".dll")
                or name.startswith(("rust-gdb", "rust-lldb", "rust-windbg")))
    # lib/**
    if parts[1] != "rustlib":
        return name.endswith(".rlib") or name.endswith(".rmeta")  # lib/libstd-*.rlib, lib/self-contained
    rest = parts[2:]
    if not rest:
        return True
    if rest[0] != TARGET:
        # other targets (wasm32, gnu) and rustlib/etc: the debugger visualizers are tiny,
        # the per-target trees are 93 MB each and this toolchain only builds one target.
        return rest[0] == "etc"
    if len(rest) >= 2 and rest[1] == "bin":
        return name == "llvm-profdata.exe"
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--toolchain", required=True, help="a rustup toolchain directory")
    ap.add_argument("--out", required=True, help="where to write the trimmed copy")
    a = ap.parse_args()

    src = pathlib.Path(a.toolchain).resolve()
    dst = pathlib.Path(a.out).resolve()
    if not (src / "bin" / "cargo.exe").exists():
        raise SystemExit(f"not a windows-msvc rust toolchain (no bin/cargo.exe): {src}")
    if not (src / "lib" / "rustlib" / TARGET / "bin" / "llvm-profdata.exe").exists():
        raise SystemExit(f"toolchain lacks lib/rustlib/{TARGET}/bin/llvm-profdata.exe: add "
                         f"`rustup component add llvm-tools-preview` — PGO cannot merge profiles "
                         f"without it")
    if dst.exists():
        shutil.rmtree(dst)

    files = 0
    total = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = pathlib.PurePosixPath(p.relative_to(src).as_posix())
        if not keep(rel, p.name):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        files += 1
        total += p.stat().st_size
    print(f"trimmed {src.name}: {files} files, {total / 1024 / 1024:.1f} MB -> {dst}")
    for sub in ("bin", "lib", "share", "etc"):
        d = dst / sub
        if d.is_dir():
            n = sum(1 for f in d.rglob("*") if f.is_file())
            b = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            print(f"  {sub:<6} {n:>6} files  {b / 1024 / 1024:>8.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
