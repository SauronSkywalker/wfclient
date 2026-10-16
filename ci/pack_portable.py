"""Assemble the release zip: embedded CPython + TUI deps + client src + patched engine.

    python ci/pack_portable.py --engine dist/wfsim-core.exe --identity dist/engine-identity.json

Layout of the zip (identical to the hand-made portable packages):

    run-portable.bat          double-click launcher
    README-便携包.md           usage / how to update / what is inside
    src/                      client (protocol 8 only, batch + idle recycle included)
    data/config.json          runtime knobs; github_pat is always blanked
    engine/wfsim-core.exe     the MSVC+PGO engine built by ci/build_engine.py
    engine/engine-identity.json
    engine-src/state.json     pins the engine revision so the client starts offline
    python/                   python-3.11.9-embed-amd64 + site-packages

Nothing else: no toolchain/, no build-cache/, no engine-src/work, no logs.  Consequence:
this package cannot rebuild an engine by itself (that needs MSVC + PGO), it runs the one
that CI built; when upstream moves the board's engine id, download the next release zip.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PY_URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
PYTH = """python311.zip
.
..
Lib\\site-packages
import site
"""


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
    stamp = a.stamp or __import__("time").strftime("%Y%m%d-%H%M")

    eng_src = (root / a.engine) if not pathlib.Path(a.engine).is_absolute() else pathlib.Path(a.engine)
    if not eng_src.exists():
        raise SystemExit(f"engine not found: {eng_src}")
    identity = {}
    id_src = (root / a.identity) if not pathlib.Path(a.identity).is_absolute() else pathlib.Path(a.identity)
    if id_src.exists():
        identity = json.loads(id_src.read_text(encoding="utf-8"))
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

    # 2) client + config (never ship a token inside a release asset)
    shutil.copytree(root / "src", build / "src")
    (build / "data").mkdir()
    cfg = json.loads((root / "data" / "config.json").read_text(encoding="utf-8"))
    cfg["github_pat"] = ""
    cfg["known_engine"] = engine_id
    cfg["engine_exe"] = "engine\\wfsim-core.exe"
    (build / "data" / "config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for name in ("run-portable.bat", "README.txt", "README-便携包.md"):
        if (root / name).exists():
            shutil.copy2(root / name, build / name)

    # 3) engine + identity + the state.json that lets the client start offline
    (build / "engine").mkdir()
    shutil.copy2(eng_src, build / "engine" / "wfsim-core.exe")
    (build / "engine" / "engine-identity.json").write_text(
        json.dumps(identity, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (build / "engine-src").mkdir()
    state = {
        "engine": engine_id,
        "release": identity.get("release_online") or identity.get("release") or "",
        "server_commit": identity.get("commit") or "",
        "sha256": identity.get("sha256") or "",
        "optimizer_tree_sha": identity.get("optimizer_tree_sha") or "",
        "patches_sha": identity.get("patches_sha") or "",
        "installed": f"ci:{stamp}",
        "prebuilt": True,
    }
    (build / "engine-src" / "state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 4) zip
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
                      "engine": engine_id, "stamp": stamp,
                      "files": sum(1 for _ in build.rglob('*') if _.is_file())}, indent=2))
    if not a.keep_build:
        shutil.rmtree(build, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
