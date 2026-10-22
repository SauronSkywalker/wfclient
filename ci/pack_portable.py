"""Assemble the release zip: embedded CPython + TUI deps + client src + patched engine.

    python ci/pack_portable.py --engine dist/wfsim-core.exe --identity dist/engine-identity.json

Layout of the zip (a portable folder is a SET OF GENERATIONS plus one pointer):

    python-<hash>/            interpreter + site-packages, immutable
    src-<hash>/               client code, immutable
    engine-<hash>/            wfsim-core.exe + engine-identity.json, immutable
    engine-src/state.json     pins the engine revision so the client starts offline
    current.txt.next          four lines: python, src, engine, launcher
    run-portable-<stamp>.bat  this release's launcher (a NEW FILE every release)
    启动.bat / run-portable.bat   the same frozen forwarder under both names: it reads
                              line 4 of the pointer and calls that launcher
    README-便携包.md / 自构建说明.md / README.txt

WHY THE NAMES CARRY A HASH.  The old package was unpacked OVER the live folders:
the running `python.exe` is locked on Windows, so those files were "skipped" and
the folder ended up half old (a client running new `app.py` against an old
`fleet.py` — the frozen-header incident of 2026-10-22).  A generation directory is
never overwritten: its name IS its content, so a new release lands beside the old
one, and the single commit point is `current.txt` (one atomic rename of one small
file).  An interrupted update therefore leaves the previous generation running,
not a mixture of two.  The installer also skips a generation whose hash is already
present — so a release that changes only `src/` writes ~0.4 MB, not 37 MB.

NOTHING ELSE.  In particular **no data/**: config.json is runtime state that belongs
to the machine that runs the client, not to the machine that packed it.  The first
launch creates a machine-specific one (config.load_or_create -> machine_default_config,
so it cannot inherit the packer's engine_cores/work_lanes/free port), and an old
data/ copied over a new package cannot resurrect a stale manual engine pin either.

No toolchain/, no build-cache/, no engine-src/work, no logs.  Consequence:
this package cannot rebuild an engine by itself (that needs MSVC + PGO), it runs the one
that CI built; when upstream moves the board's engine id, download the next release zip.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

PY_URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
PYTH = """python311.zip
.
..
Lib\\site-packages
import site
"""

#: Written INSIDE every generation directory.  `hash` is the same number the
#: directory name carries, so an installer can tell "already here, byte for byte"
#: from "same name, damaged content" without unpacking anything.
GEN_MARKER = ".gen.json"
#: The three parts that ship as content-addressed generations.
GEN_KINDS = ("python", "src", "engine")
#: Four lines in this order; the launcher reads 1/2, the dispatcher reads 4.
POINTER_LINES = ("python", "src", "engine", "launcher")


def sh(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def get_embed_python(cache: pathlib.Path, version: str) -> pathlib.Path:
    zip_path = cache / f"python-{version}-embed-amd64.zip"
    if not zip_path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        print(f"downloading {PY_URL.format(v=version)}")
        urllib.request.urlretrieve(PY_URL.format(v=version), zip_path)
    print(f"embed python {zip_path} ({zip_path.stat().st_size:,} bytes)")
    return zip_path


def dir_digest(folder: pathlib.Path) -> str:
    """The number a generation directory is named by.

    MUST stay identical to `src/sync_engine.py:dir_digest` — that is the whole
    contract between the packer (which names the directory) and the installer
    (which decides "this generation is already here").  Sorted relative paths and
    the sha256 of each file; twelve hex digits is plenty for a handful of
    generations and keeps the paths readable.
    """
    root = pathlib.Path(folder)
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        if rel == GEN_MARKER:
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:12]


def stamp_generation(build: pathlib.Path, kind: str, extra: dict | None = None) -> str:
    """Rename `build/<kind>` to `<kind>-<hash>/` and write its `.gen.json`.

    The hash is taken BEFORE the marker exists (the marker is excluded from it
    anyway), so the name and the recorded number agree by construction.
    """
    src = build / kind
    digest = dir_digest(src)
    name = f"{kind}-{digest}"
    dst = build / name
    os.replace(src, dst)                      # same volume, atomic, no copy
    files = [p for p in dst.rglob("*") if p.is_file()]
    info = {
        "kind": kind,
        "hash": digest,
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if extra:
        info.update(extra)
    (dst / GEN_MARKER).write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8", newline="\n")
    print(f"  {name}/  {info['files']} files, {info['bytes']:,} bytes")
    return name


def read_template(root: pathlib.Path, name: str) -> str:
    p = root / "ci" / "portable" / name
    if not p.is_file():
        raise SystemExit(f"missing launcher template: {p}")
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--engine", default="dist/wfsim-core.exe")
    ap.add_argument("--identity", default="dist/engine-identity.json")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--build-dir", default="",
                    help="staging dir to assemble into (default: <out>/portable-<stamp>)")
    ap.add_argument("--python-version", default="3.11.9")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--keep-build", action="store_true")
    a = ap.parse_args()

    root = pathlib.Path(a.root).resolve()
    out = (root / a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    stamp = a.stamp or time.strftime("%Y%m%d-%H%M")

    eng_src = (root / a.engine) if not pathlib.Path(a.engine).is_absolute() else pathlib.Path(a.engine)
    if not eng_src.exists():
        raise SystemExit(f"engine not found: {eng_src}")
    identity = {}
    id_src = (root / a.identity) if not pathlib.Path(a.identity).is_absolute() else pathlib.Path(a.identity)
    if id_src.exists():
        identity = json.loads(id_src.read_text(encoding="utf-8-sig"))
    engine_id = identity.get("engine_online") or identity.get("engine") or "unknown"

    build = pathlib.Path(a.build_dir).resolve() if a.build_dir else out / f"portable-{stamp}"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    # 1) embedded python + dependencies
    emb = get_embed_python(out / ".cache", a.python_version)
    with zipfile.ZipFile(emb) as z:
        z.extractall(build / "python")
    (build / "python" / "python311._pth").write_text(PYTH, encoding="utf-8", newline="\n")
    sh([sys.executable, "-m", "pip", "install", "-r", str(root / "ci" / "requirements.txt"),
        "--target", str(build / "python" / "Lib" / "site-packages"),
        "--no-warn-script-location", "--disable-pip-version-check", "--quiet"])
    shutil.rmtree(build / "python" / "Lib" / "site-packages" / "bin", ignore_errors=True)

    # 2) client source only.  NOT data/: the client writes a machine-specific
    #    config.json on its first start (see the module docstring), and shipping
    #    the packer's copy is how a user ends up with someone else's CPU list or a
    #    stale manual engine pin.  Nothing in the zip needs data/ to exist: the
    #    engine identity lives in engine-src/state.json.
    shutil.copytree(root / "src", build / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 3) engine + identity + the state.json that lets the client start offline
    (build / "engine").mkdir()
    shutil.copy2(eng_src, build / "engine" / "wfsim-core.exe")
    core_sha = hashlib.sha256((build / "engine" / "wfsim-core.exe").read_bytes()).hexdigest()
    (build / "engine" / "engine-identity.json").write_text(
        json.dumps(identity, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 4) THE GENERATIONS.  Names are hashes, so an installer can skip what it
    #    already has and can never mix two releases in one folder.
    print("generations:")
    gens = {
        "python": stamp_generation(build, "python"),
        "src": stamp_generation(build, "src"),
        "engine": stamp_generation(build, "engine",
                                   {"engine": engine_id, "sha256": core_sha}),
    }

    # 5) the pointer, this release's launcher, and the fixed dispatcher.
    launcher = f"run-portable-{stamp}.bat"
    text = (read_template(root, "launcher.bat.in")
            .replace("@STAMP@", stamp)
            .replace("@PY@", gens["python"])
            .replace("@SRC@", gens["src"]))
    (build / launcher).write_text(text, encoding="utf-8", newline="\r\n")
    # The forwarder under BOTH names: the new one (`启动.bat`) and the name the
    # docs and shortcuts have always used, so a hand-unpacked copy starts either
    # way and an in-place update leaves the old file alone (same content).
    forwarder = read_template(root, "启动.bat")
    (build / "启动.bat").write_text(forwarder, encoding="utf-8", newline="\r\n")
    (build / "run-portable.bat").write_text(forwarder, encoding="utf-8",
                                            newline="\r\n")
    (build / "current.txt.next").write_text(
        "".join(f"{gens[k]}\n" for k in ("python", "src", "engine")) + f"{launcher}\n",
        encoding="utf-8", newline="\n")

    # 自构建说明.md 也要进包：README-便携包.md 里链到它，而且"想自己编译的人"
    # 正是从便携包这一页往下看的。它只有 8 KB，不影响包体。
    for name in ("README.txt", "README-便携包.md", "自构建说明.md"):
        if (root / name).exists():
            shutil.copy2(root / name, build / name)

    (build / "engine-src").mkdir()
    state = {
        "engine": engine_id,
        "release": identity.get("release_online") or identity.get("release") or "",
        "server_commit": identity.get("commit") or "",
        "sha256": core_sha or identity.get("sha256") or "",
        "optimizer_tree_sha": identity.get("optimizer_tree_sha") or "",
        "patches_sha": identity.get("patches_sha") or "",
        "installed": f"ci:{stamp}",
        # Which generation this package considers current (for the log/设置页;
        # the authoritative pointer is current.txt.next next to it).
        "generations": dict(gens, launcher=launcher),
        # The zip ships no toolchain/, no patches/ and no core-host-template/,
        # so it can only DOWNLOAD a core.  Saying so in the state is what keeps
        # sync_engine from hashing the empty patch set as "our patches changed"
        # and pausing the fleet to run a cargo that is not in the zip.
        "core_source": "prebuilt",
        "prebuilt": True,
    }
    (build / "engine-src" / "state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # The package is valid only if the pointer names generations that agree with
    # their own `.gen.json` and the launcher it names is really here.  Asserted
    # here, once, so both publishing lines get the same guard.
    for _kind in GEN_KINDS:
        _dir = build / gens[_kind]
        if not _dir.is_dir():
            raise SystemExit(f"generation {gens[_kind]} is missing")
        _marker = json.loads((_dir / GEN_MARKER).read_text(encoding="utf-8-sig"))
        _files = [x for x in _dir.rglob("*") if x.is_file() and x.name != GEN_MARKER]
        _got = (len(_files), sum(x.stat().st_size for x in _files))
        if _got != (_marker.get("files"), _marker.get("bytes")):
            raise SystemExit(f"{gens[_kind]} disagrees with its {GEN_MARKER}: {_got}")
        if _marker.get("hash") != gens[_kind].split("-", 1)[1]:
            raise SystemExit(f"{gens[_kind]} carries a different hash")
    if not (build / launcher).is_file():
        raise SystemExit(f"the pointer names {launcher}, which is not in the package")

    # 6) zip
    #
    # The name is the engine id and nothing else, on purpose: the release tag is
    # already `portable-<engine id>`, so a stamp in the file name would make every
    # scheduled run append yet another ~22 MB asset to the same tag instead of
    # replacing the one that describes that engine (measured: 4 zips / 90 MB after
    # one day).  Pass --stamp explicitly when you want to keep several side by side.
    suffix = f"-{stamp}" if a.stamp else ""
    zip_path = out / f"wfsim-portable-{engine_id}{suffix}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(build.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(build).as_posix())
    print(json.dumps({"zip": str(zip_path), "bytes": zip_path.stat().st_size,
                      "engine": engine_id, "stamp": stamp, "launcher": launcher,
                      "generations": gens,
                      "files": sum(1 for _ in build.rglob('*') if _.is_file())}, indent=2))
    if not a.keep_build:
        shutil.rmtree(build, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
