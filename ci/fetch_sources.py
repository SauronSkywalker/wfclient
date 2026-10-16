"""CI step 1/2 - assemble the engine source tree: latest upstream sources + our patches.

    python ci/fetch_sources.py [--root .] [--ref main]

Writes ``<root>/engine-src/{work,core-host-template,patches,state.json,ci-identity.json}``.
It calls the client's own ``sync_engine`` on purpose: CI then fetches and patches exactly the
same way a user's portable package does, so the built engine id matches the one the board
server is serving.

The tree is assembled from
  * upstream ``magenie33/wfsim@<ref>`` (engine/, webapi/, optimizer/, tools/, data/, Cargo.*)
  * ``ci/core-host-template/``  (the client-side crate wrapper, copied into work/core-host/)
  * ``ci/patches/*.patch``      (our four patches, applied with the bundled patch.exe)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="package/repo root (holds src/ and ci/)")
    ap.add_argument("--ref", default="main", help="upstream ref to sync")
    a = ap.parse_args()

    root = pathlib.Path(a.root).resolve()
    sys.path.insert(0, str(root / "src"))
    import sync_engine as se  # noqa: E402  (needs src/ on sys.path)

    # CI keeps the patches/template under ci/, the client keeps them under engine-src/.
    for src, dst in ((root / "ci" / "patches", se.PATCHES),
                     (root / "ci" / "core-host-template", se.TEMPLATE)):
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"staged {src.relative_to(root)} -> {dst}")

    # The runner's own GITHUB_TOKEN (5000 req/h) instead of a personal PAT (60 req/h anonymous).
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("WFSIM_GITHUB_PAT") or ""
    if token:
        se.configured_pat = lambda: token
        print("using GITHUB_TOKEN for GitHub requests")
    else:
        print("WARNING: no GITHUB_TOKEN, falling back to the anonymous 60 req/h limit")

    se.ensure_worktree()
    total, changed, commit = se.sync_sources(a.ref)
    print(f"upstream {a.ref}: {total} build files, {changed} changed, commit={commit}")

    se.ensure_build_profiles()
    print("build profiles: ok (maxperf/devfast)")
    se.apply_patches()

    rel = se.release_info() or {}
    identity = {
        "ref": a.ref,
        "commit": commit,
        "engine_online": rel.get("engine"),
        "release_online": rel.get("release"),
        "optimizer_tree_sha": se.optimizer_tree_sha(),
        "patches_sha": se.patches_sha(),
        "patches": sorted(p.name for p in se.PATCHES.glob("*.patch")),
    }
    out = se.STATE.parent / "ci-identity.json"
    out.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    print(json.dumps(identity, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
