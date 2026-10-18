# -*- coding: utf-8 -*-
"""Write `client-manifest.json` -- which CLIENT sources a portable zip carries.

The zip says which CORE it carries; this says which `src/*.py` it carries.  A
prebuilt copy follows exactly one update signal, the served engine id, and that
id does not move when only `src/` does -- so without this file a fix that
changed no engine was invisible to every copy that did not replace its sources
by hand (measured 2026-10-18).

The manifest is published beside the zip on the same `portable-<engine>` tag
(overwritten in place like the zip), so a copy can hash its own sources against
it and learn it is behind.

Usage:
    python ci/make_manifest.py --out dist/client-manifest.json \
        [--engine 017f5b3ad787] [--zip dist/wfsim-portable-017f5b3ad787.zip] \
        [--repo-root .]

Without `--zip` the newest `dist/wfsim-portable-*.zip` is used when there is
exactly one; the asset digest is best-effort and never fails the build.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

#: Every Python source the release ships.  DERIVED, not listed: a new
#: `src/*.py` enters the manifest with no edit here, and a file that never
#: ships can never be mentioned.  The client compares whatever the manifest
#: names, so the two sides cannot drift apart.
def source_names(root: pathlib.Path) -> tuple[str, ...]:
    return tuple(f"src/{p.name}" for p in sorted((root / "src").glob("*.py")))


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def head_commit(root: pathlib.Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             check=True, text=True)
        return out.stdout.strip()
    except Exception:                                            # noqa: BLE001
        return ""


def pick_zip(where: pathlib.Path, given: str) -> pathlib.Path | None:
    if given:
        p = pathlib.Path(given)
        return p if p.is_file() else None
    found = sorted(where.glob("wfsim-portable-*.zip"))
    return found[0] if len(found) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist/client-manifest.json")
    ap.add_argument("--engine", default="")
    ap.add_argument("--zip", default="")
    ap.add_argument("--repo-root", default=".")
    a = ap.parse_args()

    root = pathlib.Path(a.repo_root).resolve()
    src: dict[str, str] = {}
    for name in source_names(root):
        p = root / name
        if not p.is_file():
            raise SystemExit(f"missing source for the manifest: {name}")
        src[name] = sha256_of(p)

    info = {
        "schema": 1,
        "repo": "SauronSkywalker/wfclient",
        "commit": head_commit(root),
        "built_at": datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": str(a.engine or ""),
        "src": src,
    }
    z = pick_zip(root / "dist", a.zip or "")
    if z is not None:
        info["asset"] = z.name
        info["asset_sha256"] = sha256_of(z)
    else:
        print("no zip beside the manifest: the asset digest is left out")

    out = root / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(info, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
