"""Assemble the self-build dependency pack: everything a user needs except Visual Studio.

    python ci/make_deps.py --toolchain <trimmed rust msvc> --registry <cargo registry> \
        --patch-bin <dir with patch.exe> --profdata <default.profdata> --work engine-src/work \
        --engine <engine id> --out dist/wfclient-deps-<engine>.zip

Why it exists: the portable package carries a prebuilt x86-64-v3 engine and *no* toolchain,
so it can only download engines.  The user who wants the *fastest* engine on their own CPU
(native + PGO) downloads this pack and unpacks it into the portable folder; the client then
sees the pack, shows the compile button and can build locally.  The only thing the pack does
NOT contain is Visual Studio itself: cl.exe / link.exe and the CRT + SDK import libraries are
not redistributable (Microsoft grants that for the runtime DLLs only), so the user installs
the newest VS (2026; 2022 works too) and the build finds it through vswhere.

Layout of the zip -- unpacked into the portable package root, so it merges with it:

    build-deps.json            marker the client looks for (schema, engine id, toolchain)
    build-deps/README-自编译依赖.md
    build-deps/bin/patch.exe   msys patch (the client applies ci/patches/*.patch with it)
    build-deps/cargo/registry  vendored crates -> CARGO_HOME at build time (offline builds)
    build-deps/pgo/default.profdata   the PGO profile of this revision (skip the training pass)
    toolchain-msvc/            trimmed rust 1.97.1-x86_64-pc-windows-msvc + llvm-profdata
    engine-src/work/           upstream sources with ci/patches/*.patch already applied
    ci/                        the patches, the build scripts and the frozen corpora
    build-engine.bat           double-clickable: builds the native engine and installs it

Measured on the dev machine: the trimmed toolchain is 346 MB unpacked (rustc_driver.dll alone
is 197 MB) and this zip lands around 150 MB; a native+PGO build from it takes ~2.5 min.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import zipfile

SKIP_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db"}
SKIP_SUFFIX = (".pyc", ".pyo", ".pdb", ".profraw")


def rel_files(root: pathlib.Path, *, keep_suffix: tuple[str, ...] = ()):
    """Every file below root, sorted, minus caches -- and minus *.pdb (84 MB of them)."""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_NAMES for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX and not p.name.endswith(keep_suffix):
            continue
        out.append(p)
    return out


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024


def add_tree(zf: zipfile.ZipFile, src: pathlib.Path, prefix: str, *, verbose: bool = True) -> int:
    files = rel_files(src)
    total = sum(p.stat().st_size for p in files)
    if verbose:
        print(f"  + {prefix}/  {len(files)} files, {human(total)}", flush=True)
    for p in files:
        zf.write(p, f"{prefix}/{p.relative_to(src).as_posix()}")
    return len(files)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--toolchain", required=True, help="trimmed rust msvc toolchain directory")
    ap.add_argument("--registry", required=True, help="cargo registry directory (cache+src+index)")
    ap.add_argument("--patch-bin", default="",
                    help="directory holding patch.exe + msys-2.0.dll; omit when the shipped "
                         "sources are already patched and will never be re-synced (fresh CI runs, "
                         "or the native line, which never patches)")
    ap.add_argument("--profdata", default="",
                    help="default.profdata of this revision; omit to ship no profile "
                         "(the client then trains one locally, about 2 minutes slower)")
    ap.add_argument("--work", default="engine-src/work", help="patched upstream sources")
    ap.add_argument("--ci", default="ci", help="the ci/ tree to ship")
    ap.add_argument("--engine", default="", help="engine id this pack is for")
    ap.add_argument("--toolchain-name", default="1.97.1-x86_64-pc-windows-msvc")
    ap.add_argument("--out", default="dist/wfclient-deps.zip")
    a = ap.parse_args()

    root = pathlib.Path(a.root).resolve()
    toolchain = pathlib.Path(a.toolchain).resolve()
    registry = pathlib.Path(a.registry).resolve()
    patch_bin = pathlib.Path(a.patch_bin).resolve() if a.patch_bin else None
    profdata = pathlib.Path(a.profdata).resolve() if a.profdata else None
    work = (root / a.work).resolve() if not pathlib.Path(a.work).is_absolute() else pathlib.Path(a.work)
    ci = (root / a.ci).resolve() if not pathlib.Path(a.ci).is_absolute() else pathlib.Path(a.ci)
    out = pathlib.Path(a.out)
    out = (root / out).resolve() if not out.is_absolute() else out
    out.parent.mkdir(parents=True, exist_ok=True)

    for name, p in (("toolchain", toolchain), ("registry", registry),
                    ("work", work), ("ci", ci)):
        if not p.exists():
            raise SystemExit(f"missing {name}: {p}")
    if patch_bin is not None and not patch_bin.exists():
        raise SystemExit(f"missing patch-bin: {patch_bin}")
    if profdata is not None and not profdata.exists():
        raise SystemExit(f"missing profdata: {profdata}")
    cargo = toolchain / "bin" / "cargo.exe"
    if not cargo.exists():
        raise SystemExit(f"not a rust toolchain (no bin/cargo.exe): {toolchain}")
    if not (toolchain / "lib" / "rustlib" / "x86_64-pc-windows-msvc" / "bin" / "llvm-profdata.exe").exists():
        raise SystemExit(f"toolchain lacks llvm-profdata.exe (PGO needs it): {toolchain}")
    for f in ("patch.exe", "msys-2.0.dll"):
        if patch_bin is not None and not (patch_bin / f).exists():
            raise SystemExit(f"missing {f} in {patch_bin}")
    if not (work / "Cargo.toml").exists():
        raise SystemExit(f"not a source tree (no Cargo.toml): {work}")

    marker = {
        "schema": 1,
        "kind": "wfclient-selfbuild-deps",
        "engine": a.engine,
        "toolchain": a.toolchain_name,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "requires": "Visual Studio 2026 (2022 or newer also works) with the C++ workload",
        "pgo": profdata is not None,
        "patch_tool": patch_bin is not None,
        "entries": {
            "toolchain": "toolchain-msvc",
            "cargo_home": "build-deps/cargo",
            "patch": "build-deps/bin" if patch_bin is not None else "",
            "profdata": "build-deps/pgo/default.profdata" if profdata is not None else "",
            "work": "engine-src/work",
            "ci": "ci",
            "launcher": "build-engine.bat",
        },
    }

    print(f"packing {out.name}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("build-deps.json", json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
        print("  + build-deps.json")
        n = 0
        n += add_tree(zf, toolchain, "toolchain-msvc")
        n += add_tree(zf, registry, "build-deps/cargo/registry")
        if patch_bin is not None:
            for f in ("patch.exe", "msys-2.0.dll"):
                zf.write(patch_bin / f, f"build-deps/bin/{f}")
            n += 2
            print("  + build-deps/bin/  2 files")
        else:
            print("  - no patch.exe (the native line compiles already-patched sources)")
        if profdata is not None:
            zf.write(profdata, "build-deps/pgo/default.profdata")
            n += 1
            print("  + build-deps/pgo/default.profdata")
        else:
            print("  - no PGO profile (the client will train one locally)")
        n += add_tree(zf, work, "engine-src/work")
        n += add_tree(zf, ci, "ci")
        readme = pathlib.Path(__file__).with_name("deps") / "README-自编译依赖.md"
        if readme.exists():
            zf.write(readme, "build-deps/README-自编译依赖.md")
            n += 1
        bat = pathlib.Path(__file__).with_name("deps") / "build-engine.bat"
        if bat.exists():
            zf.write(bat, "build-engine.bat")
            n += 1

    size = out.stat().st_size
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"\n{n} files -> {out}  {human(size)}")
    print(f"sha256={sha}")
    side = out.with_suffix(".json")
    side.write_text(json.dumps({**marker, "zip": out.name, "bytes": size, "sha256": sha,
                                "files": n}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"identity -> {side}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
