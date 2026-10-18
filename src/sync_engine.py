# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Incremental updater for the portable WFSim compute core.

The updater does four things, in order:

  1. read https://wfsim.app/release.json and compare its engine id with the
     engine id last installed beside this script;
  2. fetch only the source trees the minimal core needs:
     engine/, webapi/, optimizer/, data/, Cargo.toml and Cargo.lock;
  3. apply the local optimisation patch series;
  4. build with the bundled GNU Rust toolchain and install wfsim-core.exe.

Every file is content-hashed.  A file whose bytes did not change is never
rewritten, so Cargo's mtimes stay stable and incremental builds stay useful.
The build directory, Cargo home and TEMP all live inside this portable folder.
"""
from __future__ import annotations

import os
import sys

# The portable Python uses a ._pth file, which does NOT put the running
# script's folder on sys.path.  app.py already does this for itself; this
# script is also started as a SUBPROCESS (`python -B sync_engine.py`), where
# that bootstrap is missing — so `import cores`/`import paths` failed on any
# copy whose src folder was not already on PYTHONPATH.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse
import concurrent.futures
import functools
import hashlib
import re
import json
import pathlib
import re
import shutil
import socket
import subprocess
import tarfile
import time
import zipfile
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

import cores
import paths

ROOT = pathlib.Path(paths.ROOT)
WORK = pathlib.Path(paths.ENGINE_SRC) / "work"
TEMPLATE = pathlib.Path(paths.ENGINE_SRC) / "core-host-template"
PATCHES = pathlib.Path(paths.ENGINE_SRC) / "patches"
STATE = pathlib.Path(paths.ENGINE_SRC) / "state.json"
STAGED_DIR = pathlib.Path(paths.BUILD) / "staged"
STAGED_CORE = STAGED_DIR / "wfsim-core.exe"
PENDING = STAGED_DIR / "pending.json"
#: entity tags, so an unchanged `optimizer/src` can be checked for free
ETAG_CACHE = pathlib.Path(paths.ENGINE_SRC) / "etags.json"
TOOLCHAIN = pathlib.Path(paths.TOOLCHAIN)
RUST = TOOLCHAIN / "rust"
CARGO_HOME = TOOLCHAIN / "cargo"
TOOL_BIN = TOOLCHAIN / "bin"
BUILD = pathlib.Path(paths.BUILD)
TARGET = BUILD / "target"
TMP = BUILD / "tmp"
ENGINE = pathlib.Path(paths.ENGINE)
CONFIG = pathlib.Path(paths.CONFIG)

RELEASE_URL = "https://wfsim.app/release.json"
REPO = "magenie33/wfsim"
GITHUB_RELEASE_URL = f"https://raw.githubusercontent.com/{REPO}/main/site/release.json"
GITHUB_ENGINES_URL = f"https://api.github.com/repos/{REPO}/contents/site/engines?ref=main"
CATALOG_CACHE = pathlib.Path(paths.ENGINE_SRC) / "engine-catalog.json"
UA = "wfsimpp-portable-updater/1.0"
PREFIXES = ("engine/", "webapi/", "optimizer/", "tools/", "data/")
TOP_FILES = ("Cargo.toml", "Cargo.lock")
WORKSPACE_MEMBERS = 'members = ["core-host", "engine", "optimizer", "webapi", "tools/gas-macro"]'
#: The Python client speaks this protocol; the Rust core is arithmetic only.
CLIENT_PROTOCOL = 7

#: THE TWO UPDATE LINES ARE TWO DIFFERENT RELEASE PACKAGES.
#:
#: "prebuilt" — the CI-built portable zip.  It ships a compiled core, no
#:   `toolchain/` and no `engine-src/work`, so the only way it can ever take a
#:   new core is to DOWNLOAD one: `releases/download/engine-<id>/`.
#: "local"   — a copy that carries the bundled rust toolchain and rebuilds from
#:   synced upstream sources plus our patches.
#:
#: Which one this copy is has to be a property of the package, not a guess made
#: at update time: a portable zip that tries to build pauses the fleet, calls
#: cargo that is not there, fails on a missing core-host template, and retries
#: every five minutes forever (measured on the first CI-built zip, 2026-10-17).
CORE_SOURCE_PREBUILT = "prebuilt"
CORE_SOURCE_LOCAL = "local"

#: Where the prebuilt line downloads from.  Overridable with data/config.json
#: `update_repo`, so a mirror or a personal fork needs no code change.
UPDATE_REPO_DEFAULT = "SauronSkywalker/wfclient"


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def configured_pat() -> str:
    """The user-controlled GitHub PAT.  Never log or return it for display."""
    return str(load_config().get("github_pat") or "").strip()


def update_repo() -> str:
    """The repository the prebuilt line downloads cores from."""
    return str(load_config().get("update_repo") or "").strip() or UPDATE_REPO_DEFAULT


def core_source() -> str:
    """Which of the two update lines this copy belongs to.

    The state file decides it, and a copy that predates the key is judged by
    what is actually on disk: no bundled cargo, or no patch set, means it cannot
    compile a core however it was labelled — so it is a prebuilt copy.
    """
    state = load_state()
    src = str(state.get("core_source") or "").strip()
    if src in (CORE_SOURCE_PREBUILT, CORE_SOURCE_LOCAL):
        return src
    if state.get("prebuilt"):
        return CORE_SOURCE_PREBUILT
    if not (RUST / "bin" / "cargo.exe").exists() or not PATCHES.is_dir():
        return CORE_SOURCE_PREBUILT
    return CORE_SOURCE_LOCAL


def is_github_url(url: str) -> bool:
    return (url.startswith("https://api.github.com/")
            or url.startswith("https://raw.githubusercontent.com/"))


def request_headers(url: str, extra: dict | None = None) -> dict:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if extra:
        headers.update(extra)
    # ONLY GitHub gets the token.  wfsim.app/release.json must never see it.
    if is_github_url(url):
        pat = configured_pat()
        if pat:
            headers["Authorization"] = f"Bearer {pat}"
    return headers


def open_github(req: urllib.request.Request, timeout: int):
    """Open a GitHub request, falling back to anonymous on a 401 token.

    A stale/revoked PAT must not make the updater unusable.  The fallback keeps
    public-repo updates working, subject to GitHub's anonymous IP quota.
    """
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        headers = {k: v for k, v in req.header_items()
                   if k.lower() != "authorization"}
        if e.code != 401 or len(headers) == len(req.header_items()):
            raise
        log("GitHub PAT 无效或已过期；回退匿名请求")
        retry = urllib.request.Request(req.full_url, data=req.data,
                                       headers=headers, method=req.get_method())
        return urllib.request.urlopen(retry, timeout=timeout)


def parse_expiration(raw: str | None) -> float | None:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        if text.endswith(" UTC"):
            text = text[:-4] + "+00:00"
        elif text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _pat_request(pat: str):
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {pat}"}
    return urllib.request.Request("https://api.github.com/rate_limit",
                                  headers=headers)


def _pat_days_left(expires) -> int | None:
    if expires is None:
        return None
    return int((expires - time.time()) // 86400)


def _pat_ok_payload(body: dict, raw, expires) -> dict:
    core = ((body.get("resources") or {}).get("core") or {})
    return {"state": "ok", "configured": True, "valid": True,
            "expires_at": expires, "expires_raw": raw or "",
            "days_left": _pat_days_left(expires),
            "remaining": core.get("remaining")}


def _pat_http_error(e) -> dict:
    state = "invalid" if e.code == 401 else "error"
    return {"state": state, "configured": True,
            "valid": False if e.code == 401 else None,
            "http_status": e.code, "error": f"HTTP {e.code}"}


def pat_status() -> dict:
    """Validate the configured PAT and read its expiration header.

    `GET /rate_limit` is available to every authenticated token and has no
    repository-permission requirement.  A 401 means the token is invalid,
    expired, or revoked; a network error is reported separately.
    """
    pat = configured_pat()
    if not pat:
        return {"state": "missing", "configured": False, "valid": None}
    req = _pat_request(pat)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
            raw = (r.headers.get("github-authentication-token-expiration")
                   or r.headers.get("GitHub-Authentication-Token-Expiration"))
            return _pat_ok_payload(body, raw, parse_expiration(raw))
    except urllib.error.HTTPError as e:
        return _pat_http_error(e)
    except Exception as e:
        return {"state": "error", "configured": True, "valid": None,
                "error": f"{type(e).__name__}: {e}"}


def log(msg: str) -> None:
    print(msg, flush=True)
    try:
        with (ROOT / "engine-src" / "sync.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def local_ip() -> str:
    """This machine's address, so a rate-limit report names the IP it hit.

    GitHub's limit is per IP, and a 403 does not say which address it counted —
    printing ours makes "somebody else on this network is using the quota"
    distinguishable from "we are".
    """
    seen = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))       # no packet leaves the machine
            seen.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        seen.append(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    out = []
    for ip in seen:
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)
    return ",".join(out) or "?"


class NotModified(Exception):
    """304 — the source has not moved, and this response costs no rate limit."""


def load_etags() -> dict:
    try:
        return json.loads(ETAG_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_etags(data: dict) -> None:
    try:
        ETAG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ETAG_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def http_json_etag(url: str, etag: str | None) -> tuple[dict, str | None]:
    """GET with `If-None-Match`, which raises `NotModified` on a 304.

    GitHub documents that a 304 does NOT count against the rate limit.  That is
    the difference between "one request every ten minutes forever" and "one
    request every ten minutes until the source actually changes" — and the
    second one cannot run a shared IP out of quota.
    """
    extra = {"If-None-Match": etag} if etag else None
    req = urllib.request.Request(url, headers=request_headers(url, extra))
    try:
        with open_github(req, 90) as r:
            return json.loads(r.read().decode("utf-8")), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise NotModified() from None
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        # A 403 body names the address GitHub counted:
        #   "API rate limit exceeded for 203.0.113.7."
        m = re.search(r"rate limit exceeded for ([0-9A-Fa-f:.]+)", body)
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else "?"
        log(f"github HTTP {e.code}  local_ip={local_ip()}  "
            f"counted_ip={m.group(1) if m else '?'}  remaining={remaining}")
        if body:
            log("  body: " + body[:200].replace("\n", " "))
        raise


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=request_headers(url))
    with open_github(req, 90) as r:
        return json.loads(r.read().decode("utf-8"))


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=request_headers(url))
    with open_github(req, 120) as r:
        return r.read()


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"blob {len(data)}\0".encode("ascii"))
    h.update(data)
    return h.hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(functools.partial(fh.read, 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, STATE)


def installed_core_matches_state() -> bool:
    """The state hash is a guard, not a claim: a copied/corrupted exe rebuilds."""
    target = ENGINE / "wfsim-core.exe"
    if not target.exists():
        return False
    expected = str((load_state() or {}).get("sha256") or "").strip()
    if not expected:
        return True
    return file_sha256(target).lower() == expected.lower()


def _until_is_future(raw: str) -> bool:
    try:
        text = str(raw or "").strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp() > time.time()
    except Exception:
        # Older snapshots may omit `until`; do not hide them on a parse miss.
        return True


def _cached_github_json(url: str, fallback: object) -> tuple[object, str]:
    """One GitHub JSON fetch with ETag reuse and a last-good fallback."""
    etags = load_etags()
    entry = etags.get(url) or {}
    old = entry.get("value", fallback)
    try:
        value, new_etag = http_json_etag(url, entry.get("etag"))
    except NotModified:
        return old, "304"
    except Exception as e:
        log(f"GitHub catalog unavailable ({type(e).__name__}: {e}); using cached value")
        return old, "fallback"
    etags = load_etags()
    etags[url] = {"etag": new_etag, "value": value}
    save_etags(etags)
    return value, "changed"


def _read_catalog_cache() -> dict | None:
    try:
        cached = json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return cached if isinstance(cached, dict) else None


def _fresh_catalog(force: bool) -> dict | None:
    if force:
        return None
    cached = _read_catalog_cache()
    if cached is None:
        return None
    try:
        stamp = float(cached.get("cached_at") or 0)
    except Exception:
        return None
    return cached if time.time() - stamp < 540.0 else None


def _catalog_engine_ids(engines) -> list[str]:
    ids: list[str] = []
    if not isinstance(engines, list):
        return ids
    for item in engines:
        name = str((item or {}).get("name") or "")
        if name.endswith(".json"):
            eid = name[:-5]
            if re.fullmatch(r"[A-Za-z0-9._-]{1,40}", eid):
                ids.append(eid)
    return ids


def _catalog_accepted(rel: dict) -> list[dict]:
    accepted: list[dict] = []
    for item in (rel.get("accepted") or []):
        if not isinstance(item, dict):
            continue
        eid = str(item.get("engine") or "")
        if not eid:
            continue
        until = str(item.get("until") or "")
        if until and not _until_is_future(until):
            continue
        accepted.append({"engine": eid, "until": until})
    return accepted


def _make_catalog(rel: dict, ids: list[str], accepted: list[dict]) -> dict:
    current = str(rel.get("engine") or "")
    # The builder appends accepted engines in served_from order, oldest first.
    reportable = [dict(x) for x in accepted]
    reportable.append({"engine": current, "until": None})
    reportable_ids = {x["engine"] for x in reportable}
    ci_only = [{"engine": eid} for eid in ids if eid not in reportable_ids]
    return {
        "source": "github",
        "cached_at": time.time(),
        "current": current,
        "accepted": accepted,
        "reportable": reportable,
        "ci_only": ci_only,
        "newest_accepted": accepted[-1]["engine"] if accepted else None,
    }


def _save_catalog(catalog: dict) -> None:
    try:
        CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_CACHE.write_text(json.dumps(catalog, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:
        pass


def github_engine_catalog(force: bool = False) -> dict:
    """The reportable engine list, read from GitHub's generated site files.

    `site/release.json.accepted` is used only to build the selectable version
    list.  The client does not classify or filter tasks by capability: it sends
    the selected id and computes exactly what the server returns.  `site/engines/`
    is a snapshot index for CI-known ids; CI-only ids are not selectable.
    """
    cached = _fresh_catalog(force)
    if cached is not None:
        return cached
    rel, _ = _cached_github_json(GITHUB_RELEASE_URL, {})
    if not isinstance(rel, dict) or not rel.get("engine"):
        cached = _read_catalog_cache()
        if cached is not None:
            return cached
        raise RuntimeError("GitHub release.json has no engine and no catalog cache exists")
    engines, _ = _cached_github_json(GITHUB_ENGINES_URL, [])
    catalog = _make_catalog(rel, _catalog_engine_ids(engines),
                            _catalog_accepted(rel))
    _save_catalog(catalog)
    return catalog


def release_info() -> dict:
    j = http_json(RELEASE_URL)
    if not j.get("engine") or not j.get("commit"):
        raise RuntimeError(f"release.json has no engine/commit: {j}")
    # release.json writes a short build commit with a trailing "+".  It is
    # kept for display only: the updater reads the public main tree, because
    # the generated site commit is not always pushed to GitHub.
    j["commit"] = str(j.get("commit") or "")
    return j


# THE ONE SIGNAL THE ENGINE ID CANNOT GIVE.  Upstream builds `engine` by
# hashing ENGINE_SOURCES = ("engine/src", "webapi/src", "wasm/src", "data");
# `optimizer/` — the riven search — is deliberately not in it.  A change to the
# search alone therefore arrives with an UNCHANGED engine id.  A git tree SHA is
# a Merkle hash over everything below a directory, so one ~2.7 KB request to
# this path covers every file the search is built from.
OPTIMIZER_SRC = "optimizer/src"


def optimizer_sha_from_tree(tree: dict) -> str | None:
    """Read `optimizer/src`'s tree SHA out of an ALREADY-FETCHED recursive tree.

    A rebuild fetches the whole main tree anyway, so recording the version it
    built from costs no extra request — and reading it from that same response
    is what keeps the recorded SHA and the synced bytes consistent.
    """
    for item in tree.get("tree") or []:
        if item.get("type") == "tree" and item.get("path") == OPTIMIZER_SRC:
            return str(item.get("sha") or "") or None
    return None


def optimizer_tree_sha() -> str:
    """The upstream `optimizer/src` tree SHA.

    AN UNCHANGED SOURCE COSTS NOTHING.  The request carries `If-None-Match`, so
    GitHub answers 304 and 304s are not counted against the rate limit — which
    is what keeps a shared or heavily-used IP from running out just because a
    client checks on a timer.
    """
    url = (f"https://api.github.com/repos/{REPO}/git/trees/"
           f"main:{urllib.parse.quote(OPTIMIZER_SRC)}")
    cache = load_etags().get(url) or {}
    etag = cache.get("etag")
    new_etag = None
    try:
        tree, new_etag = http_json_etag(url, etag)
    except NotModified:
        sha = cache.get("sha")
        if sha:
            log(f"optimizer/src unchanged (304, no rate-limit cost) "
                f"sha={str(sha)[:12]} local_ip={local_ip()}")
            return str(sha)
        tree, new_etag = http_json_etag(url, None)     # cache was lost
    sha = tree.get("sha")
    if not sha:
        raise RuntimeError(f"no tree sha for {OPTIMIZER_SRC}: {tree}")
    etags = load_etags()
    etags[url] = {"etag": new_etag, "sha": str(sha)}
    save_etags(etags)
    log(f"optimizer/src sha={str(sha)[:12]} (changed) local_ip={local_ip()}")
    return str(sha)


def head_commit(ref: str = "main") -> str:
    """The commit a branch points at right now — one small request.

    Used to decide whether a cached worktree is already at the upstream head:
    when it is, there is nothing to download at all, which is what makes CI
    runs after the first one cheap.
    """
    url = f"https://api.github.com/repos/{REPO}/commits/{urllib.parse.quote(ref)}"
    return str(http_json(url)["sha"])


def tree_for(ref: str) -> dict:
    url = f"https://api.github.com/repos/{REPO}/git/trees/{ref}?recursive=1"
    tree = http_json(url)
    if tree.get("truncated"):
        raise RuntimeError("GitHub tree response was truncated; refusing a partial sync")
    return tree


def selected_blobs(tree: dict, commit: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in tree.get("tree") or []:
        if item.get("type") != "blob":
            continue
        path = item.get("path") or ""
        if path in TOP_FILES or path.startswith(PREFIXES):
            out[path] = {
                "path": path,
                "sha": item.get("sha"),
                "size": int(item.get("size") or 0),
                "url": f"https://raw.githubusercontent.com/{REPO}/{commit}/" + urllib.parse.quote(path, safe="/"),
            }
    return out


def local_blob_sha(path: pathlib.Path) -> str | None:
    try:
        return git_blob_sha(path.read_bytes())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def work_target(rel: str) -> pathlib.Path:
    """The file a tree path names, PROVEN to sit inside the worktree.

    The listing comes from a remote server, so a path must be relative and
    must not climb out.  The components are checked rather than the resolved
    string: on Windows the same directory is reported either as ``D:\\a\\...``
    or as ``\\\\?\\D:\\a\\...`` depending on whether it exists and on any
    reparse point in the chain, so the old ``resolve()`` prefix test rejected
    an ordinary ``data/abilities/*.yaml`` on a CI runner while accepting it on
    a developer machine (run 38959289878, 2026-10-16).
    """
    rel = (rel or "").replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    # Windows silently strips a trailing space or dot from a name, which would
    # put the file somewhere other than where the listing said: refuse those
    # rather than write to a path the tree never named.
    if not parts or any(p == ".." or ":" in p or p.strip() != p for p in parts):
        raise RuntimeError(f"refusing to fetch outside the worktree: {rel!r}")
    return WORK.joinpath(*parts)


def fetch_one(item: dict) -> tuple[str, bool]:
    target = work_target(item["path"])
    if local_blob_sha(target) == item["sha"]:
        return item["path"], False
    data = http_bytes(item["url"])
    # A truncated or tampered download is a BROKEN TREE, not a smaller one:
    # verify before it can reach disk.  The truncated-body failure the fleet
    # devices see when the CDN drops a connection is exactly this shape.
    if git_blob_sha(data) != item["sha"]:
        raise RuntimeError(
            f"corrupt download for {item['path']}: sha mismatch")
    size = int(item.get("size") or 0)
    if size and len(data) != size:
        raise RuntimeError(
            f"corrupt download for {item['path']}: "
            f"{len(data)} bytes, tree says {size}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return item["path"], True


SYNC_BACKUP = "work.syncbak"

TARBALL_MIN = 64
"""Above this many files, ONE tarball beats one request per file.

Measured 2026-10-17 against upstream main: the per-file path moved the same
4,220 build files in 6.1 minutes in CI (the "fetched 3250/4220" line), while
codeload served the whole commit — 88.6 MB, 9,463 files — in 11.6 seconds on
a single connection.  Only the ~23 MB we actually build is written to disk.
The bytes are still checked against the git blob sha the tree listed, one
file at a time, so this is a transport change and not a trust change.
"""


def fetch_many_tarball(want: dict, commit: str) -> int:
    """Fetch every wanted file from one codeload tarball; returns files changed.

    `r|gz` streams the archive, so the ~65 MB of tree we do not build is never
    materialised and the whole thing is read once, in order.
    """
    url = f"https://codeload.github.com/{REPO}/tar.gz/{commit}"
    root = f"wfsim-{commit}/"
    changed = 0
    seen: set[str] = set()
    req = urllib.request.Request(url, headers=request_headers(url))
    with open_github(req, 300) as r, tarfile.open(fileobj=r, mode="r|gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            rel = member.name[len(root):] if member.name.startswith(root) else member.name
            item = want.get(rel)
            if item is None:
                continue
            seen.add(rel)
            target = work_target(rel)
            if local_blob_sha(target) == item["sha"]:
                continue
            handle = tar.extractfile(member)
            data = handle.read() if handle is not None else b""
            if git_blob_sha(data) != item["sha"]:
                raise RuntimeError(f"corrupt tarball member {rel}: sha mismatch")
            size = int(item.get("size") or 0)
            if size and len(data) != size:
                raise RuntimeError(
                    f"corrupt tarball member {rel}: {len(data)} bytes, "
                    f"tree says {size}")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            changed += 1
    missing = []
    for rel, item in want.items():
        if rel in seen:
            continue
        try:
            if local_blob_sha(work_target(rel)) == item["sha"]:
                continue
        except Exception:
            pass
        missing.append(rel)
    if missing:
        raise RuntimeError(
            f"tarball {commit[:12]} is missing {len(missing)} wanted file(s): "
            + ", ".join(sorted(missing)[:3]))
    return changed


def _fetch_each(want: dict) -> int:
    """One request per file — the small-subset path and the tarball fallback."""
    changed = 0
    fetched: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        futures = [ex.submit(fetch_one, item) for item in want.values()]
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                path, did = fut.result()
            except Exception as e:
                raise RuntimeError(f"download failed: {e}") from e
            fetched.add(path.replace(os.sep, "/"))
            changed += int(did)
            if i % 250 == 0 or i == len(futures):
                log(f"  fetched {i}/{len(futures)}")
    return changed


def _backup_root() -> pathlib.Path:
    return pathlib.Path(paths.ENGINE_SRC) / SYNC_BACKUP


def _backup_tree_snapshot(want: dict) -> None:
    """Keep the pre-sync copies of every file this pass will change.

    Cheap on purpose: only files whose sha differs are copied, and OLD trees
    it replaces.  A prune that later proves wrong has a way back without a
    full re-clone.
    """
    backup = _backup_root()
    try:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, exist_ok=True)
        n = 0
        for item in want.values():
            src = (WORK / item["path"]).resolve()
            try:
                if not src.is_file() or git_blob_sha(src.read_bytes()) == item["sha"]:
                    continue
            except Exception:
                continue
            dst = backup / item["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        log(f"backed up {n} changing file(s) for rollback")
    except Exception as e:
        log(f"backup skipped ({type(e).__name__}: {e}) — sync cannot roll back")


def _restore_sync_backup() -> None:
    """Put the pre-sync tree back; the next sync starts clean."""
    backup = _backup_root()
    if not backup.is_dir():
        log("no sync backup to restore")
        return
    for src in backup.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(backup)
        dst = WORK / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except Exception as e:
            log(f"restore {rel} failed: {type(e).__name__}: {e}")
    shutil.rmtree(backup, ignore_errors=True)
    log("restored the pre-sync tree from backup")


def ensure_worktree() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    core = WORK / "core-host"
    if not core.exists():
        if not TEMPLATE.exists():
            raise RuntimeError(f"missing core-host template: {TEMPLATE}")
        shutil.copytree(TEMPLATE, core)
        log(f"restored core-host from template")


def rewrite_workspace_members() -> None:
    p = WORK / "Cargo.toml"
    s = p.read_text(encoding="utf-8")
    # Anchor the match to the [workspace] table so a stray `members = ...`
    # in any other table is never mistaken for it.
    s2, n = re.subn(r"(\[workspace\][^\[]*?members\s*=\s*)\[[^\]]*\]",
                     r"\g<1>" + WORKSPACE_MEMBERS.split("=", 1)[1].strip(),
                     s, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError("could not rewrite [workspace].members in Cargo.toml")
    if s2 != s:
        p.write_text(s2, encoding="utf-8")
        log("rewrote Cargo.toml workspace members for the minimal core")


def _prune_tree(base: pathlib.Path, expected: set[str]) -> int:
    removed = 0
    if not base.exists():
        return removed
    for p in sorted(base.rglob("*"), reverse=True):
        if p.is_file():
            rel = p.relative_to(WORK).as_posix()
            if rel not in expected:
                p.unlink()
                removed += 1
            continue
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    return removed


def _prune_top_files(expected: set[str]) -> int:
    removed = 0
    for rel in TOP_FILES:
        p = WORK / rel
        if p.exists() and rel not in expected:
            p.unlink()
            removed += 1
    return removed


def prune_missing(expected: set[str]) -> int:
    roots = [WORK / p.rstrip("/") for p in PREFIXES]
    return (sum(_prune_tree(base, expected) for base in roots)
            + _prune_top_files(expected))


def sync_sources(ref: str = "main", want: dict | None = None) -> tuple[int, int, str | None]:
    ensure_worktree()
    tree = None
    if want is None:
        tree = tree_for(ref)
        want = selected_blobs(tree, ref)
    opt_sha = optimizer_sha_from_tree(tree) if tree else None
    log(f"upstream {ref}: {len(want)} build files")
    changed = 0
    try:
        if tree is not None and len(want) >= TARBALL_MIN:
            commit = tree.get("sha") or ref
            t0 = time.time()
            try:
                changed = fetch_many_tarball(want, commit)
                log(f"  one tarball {commit[:12]}: {len(want)} files in "
                    f"{time.time() - t0:.1f}s")
            except Exception as e:
                log(f"  tarball fetch failed ({type(e).__name__}: {e}); "
                    "falling back to one request per file")
                changed = _fetch_each(want)
        else:
            changed = _fetch_each(want)
    except Exception:
        _restore_sync_backup()
        raise
    if tree is not None:
        _backup_tree_snapshot(want)
        removed = prune_missing(set(want))
    else:
        # A caller-supplied subset: it owns the tree, so it owns the pruning.
        removed = 0
    rewrite_workspace_members()
    # Cargo will add the new workspace member to the lockfile.  The upstream
    # lockfile remains the source of dependency versions.
    log(f"source sync complete: {changed} changed, {removed} removed")
    if tree is None:
        # The caller passed a tree it had already read; ask for the one field
        # we still need rather than re-downloading the whole thing.
        opt_sha = optimizer_tree_sha()
    return changed, removed, opt_sha


def patch_already_applied() -> bool:
    try:
        mono = (WORK / "engine/src/fight/monte_carlo.rs").read_text(encoding="utf-8")
        rows = (WORK / "webapi/src/board_rows.rs").read_text(encoding="utf-8")
        sim = (WORK / "webapi/src/simulate.rs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return ("fold_parallel" in mono and "fold_runs_with" in rows
            and "simulate_merged_shard_json" in sim)


def work_stats_patch_already_applied() -> bool:
    try:
        opt = (WORK / "webapi/src/optimize.rs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return ("quick_work" in opt and '"work": total_work' in opt
            and '"builds": state.enumerated' in opt
            and '"fights": state.sims_done' in opt)


def fleet_drive_patch_already_applied() -> bool:
    try:
        main = (WORK / "core-host/src/main.rs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return ("fn optimize_drive(" in main and "fn board_row_once(" in main
            and '"/api/board/row"' in main)


def fold_pipeline_patch_already_applied() -> bool:
    """`fold_parallel` adding each run as it arrives instead of collecting all
    of them first.

    Detected by the marker the change brings with it: the streaming merger is
    the only place that names `chunk_for`, and the collected version has no
    such helper.
    """
    try:
        mono = (WORK / "engine/src/fight/monte_carlo.rs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return ("fn chunk_for(" in mono
            and "cursor.store(expect, Ordering::Release)" in mono)


def patch_command(name: str, dry_run: bool = False) -> list[str]:
    """How to apply one of our patches — with a fallback when the package's own
    `patch.exe` is not there.

    `toolchain/bin/patch.exe` ships with the portable package but NOT with a CI
    checkout (`toolchain/` is gitignored), and a GitHub runner does have git.
    Our patches are plain unified diffs with `a/`/`b/` prefixes, so `git apply
    -p1` means the same thing here, and `--check` is `patch --dry-run`.
    """
    patch = PATCHES / name
    if not patch.exists():
        raise RuntimeError(f"missing patch: {patch}")
    local = TOOL_BIN / "patch.exe"
    if local.exists():
        cmd = [str(local), "-p1", "--forward", "--batch"]
        if dry_run:
            cmd.append("--dry-run")
        return cmd + ["-i", str(patch)]
    cmd = ["git", "apply", "-p1", "--whitespace=nowarn"]
    if dry_run:
        cmd.append("--check")
    return cmd + [str(patch)]


def run_patch(name: str, dry_run: bool = False) -> None:
    env = dict(os.environ)
    env["PATH"] = str(TOOL_BIN) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(patch_command(name, dry_run), cwd=WORK, env=env,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        mode = "dry-run" if dry_run else "apply"
        raise RuntimeError(f"patch {mode} failed: {name}\n{r.stdout}\n{r.stderr}")


def apply_patch(name: str) -> None:
    # Do not modify the source tree until every hunk applies cleanly.  This is
    # the guard that keeps a future upstream engine change from half-applying
    # C1-C4 and then failing the build.
    run_patch(name, dry_run=True)
    run_patch(name, dry_run=False)
    log(f"applied {name}")


def ensure_build_profiles() -> None:
    """Append the two build profiles without depending on patch line endings."""
    p = WORK / "Cargo.toml"
    text = p.read_text(encoding="utf-8")
    if "[profile.maxperf]" not in text:
        block = """
[profile.maxperf]
inherits = "release"
opt-level = 3
lto = "fat"
codegen-units = 1
incremental = false

[profile.devfast]
inherits = "dev"
opt-level = 1
debug = false
incremental = true
codegen-units = 256
"""
        p.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
        log("added maxperf/devfast build profiles")
        return
    # The profile once carried `panic = "abort"` (added with the profile on
    # 10-10, removed here on 10-14): it turned ANY engine panic into an
    # instant 0xc0000409 process death — eight of them on 10-14 evening — and
    # silently defeated the compute-optimization patch's own `catch_unwind`,
    # which needs unwinding to work at all.  maxperf's speed comes from LTO,
    # codegen-units=1 and target-cpu=native, not from the abort; the profile
    # keeps every one of those.  Strip the line wherever it stands, so an
    # already-patched tree heals on the next sync instead of waiting for a
    # clean rebuild.
    healed = re.sub(r"(?m)^[ \t]*panic[ \t]*=[ \t]*[\"']abort[\"'][ \t]*\r?\n",
                    "", text)
    if healed != text:
        p.write_text(healed, encoding="utf-8")
        log("removed panic=abort from the maxperf profile")


def apply_patches() -> None:
    # A failed earlier run can leave patch rejects behind.  They are never
    # part of the build and would otherwise accumulate.
    for junk in WORK.rglob("*.rej"):
        junk.unlink()
    for junk in WORK.rglob("*.orig"):
        junk.unlink()
    ensure_build_profiles()
    if not patch_already_applied():
        apply_patch("wfsim-compute-optimization.patch")
        if not patch_already_applied():
            raise RuntimeError("compute patch series finished but its markers are missing")
    else:
        log("optimisation patch already applied")
    if not work_stats_patch_already_applied():
        apply_patch("wfsim-work-statistics.patch")
        if not work_stats_patch_already_applied():
            raise RuntimeError("work-statistics patch finished but its markers are missing")
    else:
        log("work-statistics patch already applied")
    # THE FLEET DRIVE: a quick search runs the page's own leader/scorer loop
    # inside the engine (core-host), so the reported `work` is the number every
    # other volunteer's client reproduces; board rows get the four board calls
    # as one.  `sync_sources` never touches core-host unless upstream changes it,
    # and this puts the loop back when it does.
    if not fleet_drive_patch_already_applied():
        apply_patch("wfsim-fleet-drive.patch")
        if not fleet_drive_patch_already_applied():
            raise RuntimeError("fleet-drive patch finished but its markers are missing")
    else:
        log("fleet-drive patch already applied")
    # THE FOLD PIPELINE: `fold_parallel` used to fight every run into one
    # `Vec<Shard>` and only then add them, so a row of a thousand runs kept a
    # thousand shards (megabytes) alive for the whole fight.  It now hands over
    # a chunk at a time and adds each run as soon as every run before it has
    # arrived — same values, same addition order, so the answer is identical to
    # the bit.  (The fixed-array `Shard` this replaces is gone: it optimised the
    # allocator, which was never the bottleneck, and its inline payload made
    # every hand-off a multi-kilobyte copy.)
    if not fold_pipeline_patch_already_applied():
        apply_patch("wfsim-fold-pipeline.patch")
        if not fold_pipeline_patch_already_applied():
            raise RuntimeError("fold-pipeline patch finished but its markers are missing")
    else:
        log("fold-pipeline patch already applied")


def find_mingw() -> pathlib.Path:
    """The MinGW-w64 bin directory cc-rs needs for `mimalloc`, or None.

    WHY: `libmimalloc-sys` vendors mimalloc's C source and builds it with
    cc-rs, and the bundled toolchain carries no C compiler at all
    (`toolchain/bin` holds patch.exe and msys-2.0.dll).  With this probe the
    package builds mimalloc on a host that HAS MinGW-w64 (6.3% faster board
    rows, 6.1% riven — bit for bit identical), and quietly falls back to
    `System` on a host that has none, instead of dying in cc-rs with
    `failed to find tool "gcc.exe"` after an upstream engine change.
    """
    for p in (pathlib.Path(r"C:\msys64\mingw64\bin"),
              pathlib.Path(r"C:\Program Files\mingw64\bin"),
              pathlib.Path(r"C:\mingw64\bin")):
        if (p / "gcc.exe").exists():
            return p
    # Last resort: a gcc of our own on PATH, but only a MinGW one — an MSYS or
    # Cygwin gcc produces executables the GNU PE target cannot link against.
    found = shutil.which("gcc")
    if found and "mingw" in found.lower():
        return pathlib.Path(found).parent
    return None


def find_llvm_ar() -> pathlib.Path:
    """An archiver that survives a non-ASCII build path, or None.

    WHY: MinGW's `ar.exe` is an MSYS program and mangles a non-ASCII path — with
    this tree under `D:\\工程\\...` the cc-rs archive step died with
    `ar.exe: D:\\????\\...\\libmimalloc.a: No such file or directory` and the
    engine never linked.  llvm-ar takes the same `cq` arguments, reads the path
    as UTF-8 and writes GNU-format archives, so prefer it where it exists.
    """
    found = shutil.which("llvm-ar")
    if found:
        return pathlib.Path(found)
    home = pathlib.Path(os.environ.get("USERPROFILE") or pathlib.Path.home())
    hits = sorted((home / ".rustup" / "toolchains").glob(
        "*/lib/rustlib/*/bin/llvm-ar.exe"))
    return hits[0] if hits else None


def build_env() -> dict:
    env = dict(os.environ)
    env.update({
        "CARGO_HOME": str(CARGO_HOME),
        "RUSTUP_HOME": str(RUST),
        "CARGO_TARGET_DIR": str(TARGET),
        "TEMP": str(TMP),
        "TMP": str(TMP),
        "HOME": str(BUILD / "home"),
        "USERPROFILE": str(BUILD / "home"),
        "RUSTFLAGS": "-C link-self-contained=yes -C target-cpu=native",
        "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER": str(
            RUST / "lib" / "rustlib" / "x86_64-pc-windows-gnu" / "bin" / "rust-lld.exe"),
        "PATH": str(RUST / "bin") + os.pathsep + str(TOOL_BIN) + os.pathsep +
                env.get("SystemRoot", r"C:\Windows") + r"\System32",
    })
    mingw = find_mingw()
    if mingw is not None:
        env["PATH"] = str(mingw) + os.pathsep + env["PATH"]
        env["CC_x86_64_pc_windows_gnu"] = str(mingw / "gcc.exe")
        env["CFLAGS_x86_64_pc_windows_gnu"] = "-O2"
        ar = find_llvm_ar()
        env["AR_x86_64_pc_windows_gnu"] = str(ar or (mingw / "ar.exe"))
    return env


def build_core(clean: bool = False) -> pathlib.Path:
    TARGET.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    (BUILD / "home").mkdir(parents=True, exist_ok=True)
    cargo = RUST / "bin" / "cargo.exe"
    if not cargo.exists():
        raise RuntimeError(f"missing bundled cargo: {cargo}")
    if clean:
        # A half-written incremental artefact makes cargo exit 101 with a
        # message that says nothing about the real cause, so a retry starts
        # from an empty target directory.
        shutil.rmtree(TARGET, ignore_errors=True)
        TARGET.mkdir(parents=True, exist_ok=True)
        log("cleaned build-cache/target for a fresh build")
    cmd = [str(cargo), "build", "--profile", "maxperf", "--target",
           "x86_64-pc-windows-gnu", "-p", "wfsim-core"]
    if find_mingw() is not None:
        # mimalloc, opt-in: cc-rs has a C compiler (see find_mingw) and the
        # feature is what puts it under the counting allocator.
        cmd += ["--features", "wfsim-core/fast-alloc"]
        log("C compiler found: fast-alloc (mimalloc) enabled")
    else:
        log("no MinGW-w64 gcc found: building with the System allocator")
    log("building: " + " ".join(cmd))
    # STREAMED AND KEPT.  Previously cargo inherited our stdout, so a failed
    # build left one line — "exit code 101" — in sync.log and the compiler's
    # own message was gone the moment the console scrolled.  Now every line is
    # forwarded live AND the last few are written to the log on failure.
    proc = subprocess.Popen(cmd, cwd=WORK, env=build_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1)
    lines: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        print(line, flush=True)          # live in the TUI's update log
        lines.append(line)
    rc = proc.wait()
    if rc != 0:
        tail = lines[-30:]
        for line in tail:
            log("  cargo| " + line)
        raise RuntimeError("cargo build failed with exit code {}:\n{}".format(
            rc, "\n".join(tail)))
    out = TARGET / "x86_64-pc-windows-gnu" / "maxperf" / "wfsim-core.exe"
    if not out.exists():
        alt = TARGET / "x86_64-pc-windows-gnu" / "release" / "wfsim-core.exe"
        if alt.exists():
            return alt
        raise RuntimeError(f"built binary not found at {out}")
    return out


def stage_core(built: pathlib.Path, release: dict, optimizer_sha: str) -> pathlib.Path:
    """Copy a finished build beside the updater, WITHOUT touching the live exe.

    The live engine can still be running while this happens.  Installation is a
    separate, idle-engine-only step (`install_staged`).
    """
    built = pathlib.Path(built)
    if not built.exists():
        raise RuntimeError(f"built binary not found at {built}")
    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, STAGED_CORE)
    digest = file_sha256(STAGED_CORE)
    pending = {
        "engine": release.get("engine"),
        "release": release.get("release"),
        "server_commit": release.get("commit"),
        "built": str(built),
        "staged": str(STAGED_CORE),
        "sha256": digest,
        "optimizer_tree_sha": optimizer_sha,
        "patches_sha": patches_sha(),
        "client_protocol": CLIENT_PROTOCOL,
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    PENDING.write_text(json.dumps(pending, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    log(f"staged {STAGED_CORE} for engine {release.get('engine')} sha256={digest[:12]}")
    return STAGED_CORE


def engine_release_base(engine: str) -> str:
    """`releases/download/engine-<id>/` on the update repo.

    A plain asset download on purpose: no API call, so the prebuilt line never
    spends the 60-requests-an-hour anonymous GitHub budget that the source line
    needs for its tree walk.
    """
    return f"https://github.com/{update_repo()}/releases/download/engine-{engine}"


def download_core(engine: str) -> pathlib.Path:
    """Fetch the published prebuilt core for `engine` and stage it.

    This is the whole of the prebuilt line's update step: no sources, no
    patches, no cargo.  The bytes were compiled and bit-gated by CI, so the
    client's job here is only to prove it received exactly those bytes: the
    identity file next to the asset carries the sha256, and the download is
    rejected unless it matches.
    """
    engine = str(engine or "").strip()
    if not engine:
        raise RuntimeError("download_core needs an engine id")
    base = engine_release_base(engine)
    identity = json.loads(http_bytes(base + "/engine-identity.json").decode("utf-8"))
    want = str(identity.get("sha256") or "").strip().lower()
    if len(want) != 64:
        raise RuntimeError(f"{base}/engine-identity.json carries no sha256")
    log(f"downloading {base}/wfsim-core.exe")
    data = http_bytes(base + "/wfsim-core.exe")
    got = sha256(data)
    if got != want:
        raise RuntimeError(f"downloaded core sha256 mismatch: {got} != {want}")
    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    built = STAGED_DIR / "downloaded.exe"
    tmp = built.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, built)
    rel = {
        "engine": engine,
        "release": identity.get("release_online") or identity.get("release"),
        "commit": identity.get("commit"),
    }
    stage_core(built, rel, str(identity.get("optimizer_tree_sha") or patches_sha()))
    log(f"downloaded core {engine} sha256={got[:12]} bytes={len(data)}")
    return STAGED_CORE


# ---------------------------------------------------------------------------
# THE WHOLE-PACKAGE UPDATE: the portable zip carries the client AND the core
# ---------------------------------------------------------------------------
#: The engine id moving is the server's signal, and the matching CLIENT may have
#: moved with it.  The zip this fetches is byte-for-byte the one the release
#: page publishes for manual download, so the automatic and the manual path
#: exercise the same artifact instead of two that can drift apart.
PORTABLE_ASSET = "wfsim-portable-{engine}.zip"

#: Never overwritten by a package update: this machine's identity and its own
#: build inputs.  data/config.json carries the account, the PAT and the verifier
#: ids — losing it would sign the user out and drop their identities.
PORTABLE_KEEP = ("data/config.json", "build-deps.json")
PORTABLE_KEEP_PREFIX = (
    "logs/",                          # this machine's own history
    "build-deps/",                    # the self-build dependency pack
    "toolchain-msvc/",                # the trimmed toolchain that pack carries
    "build-cache/",                   # cargo caches and staged cores
    "engine-src/work/",               # the synced upstream tree
    "engine/wfsim-core.native.exe",   # the engine THIS machine built for itself
    "engine/wfsim-core.exe.prev",
)
PORTABLE_UPDATE_DIR = BUILD / "update"


def portable_asset_url(engine: str) -> str:
    return (f"https://github.com/{update_repo()}/releases/download/"
            f"portable-{engine}/wfsim-portable-{engine}.zip")


#: The CLIENT-side manifest the repack job publishes beside the portable zip.
#:
#: The zip says which core it carries; this says which `src/*.py` it carries.
#: Without it, a copy can only notice a new ENGINE -- `src/` moving on a fixed
#: engine id is invisible to every update check (measured 2026-10-18: the fixes
#: shipped that morning reached only the copies whose user replaced them by
#: hand).
CLIENT_MANIFEST_ASSET = "client-manifest.json"
#: There is deliberately NO list of sources here.  `client_manifest_drift`
#: compares whatever the manifest names -- every `src/*.py` the release
#: shipped -- so a file added to `src/` needs no edit on this side, and the
#: published manifest stays the single source of truth about what shipped.
#: Where the fetched manifest is remembered between checks.
CLIENT_MANIFEST_CACHE = pathlib.Path(paths.ENGINE_SRC) / "client-manifest.json"
#: A detected-but-unapplied update, so a restart still knows about it.
PENDING_UPDATE = pathlib.Path(paths.ENGINE_SRC) / "pending-update.json"


def client_manifest_url(engine: str) -> str:
    """`releases/download/portable-<engine>/client-manifest.json`.

    A plain asset download on purpose: the tag is known here, so this needs no
    API call and no "latest" guess -- and every repack overwrites that tag's
    assets in place, so the file describes the newest client for this engine
    family.
    """
    return (f"https://github.com/{update_repo()}/releases/download/"
            f"portable-{engine}/{CLIENT_MANIFEST_ASSET}")


def client_manifest(engine: str | None = None, force: bool = False) -> dict | None:
    """The manifest for `engine` (default: the installed one), or None.

    Cached under `engine-src/` so a check every ten minutes does not fetch it
    every time.  None means "no manifest" -- an older release does not carry
    one -- and callers must read that as "no signal", never as "up to date".
    """
    engine = str(engine or load_state().get("engine") or "").strip()
    if not engine:
        return None
    if CLIENT_MANIFEST_CACHE.exists() and not force:
        try:
            cached = json.loads(
                CLIENT_MANIFEST_CACHE.read_text(encoding="utf-8-sig"))
        except Exception:
            cached = None
        if isinstance(cached, dict) and cached.get("engine") == engine:
            return cached
    try:
        info = json.loads(http_bytes(client_manifest_url(engine))
                          .decode("utf-8-sig"))
    except Exception as e:                                       # noqa: BLE001
        log(f"client manifest {engine}: not available "
            f"({type(e).__name__}: {e})")
        return None
    if not isinstance(info, dict):
        return None
    info.setdefault("engine", engine)
    try:
        CLIENT_MANIFEST_CACHE.write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return info


def client_manifest_drift(info: dict | None) -> dict:
    """Which of THIS copy's sources differ from the manifest's.

    A file we do not have, or have different bytes for, is drift.  Files we
    have that the manifest does not mention are the user's own and do not
    count: the manifest speaks only about what it ships.
    """
    out = {"have_manifest": isinstance(info, dict), "checked": 0,
           "changed": [], "missing_local": [], "commit": "", "built_at": "",
           "engine": "", "asset": "", "asset_sha256": "", "behind": False}
    if not isinstance(info, dict):
        return out
    out["commit"] = str(info.get("commit") or "")
    out["built_at"] = str(info.get("built_at") or "")
    out["engine"] = str(info.get("engine") or "")
    out["asset"] = str(info.get("asset") or "")
    out["asset_sha256"] = str(info.get("asset_sha256") or "")
    want = info.get("src")
    if not isinstance(want, dict):
        return out
    for name, sha in sorted(want.items()):
        path = ROOT / str(name)
        if not path.is_file():
            out["missing_local"].append(str(name))
            continue
        out["checked"] += 1
        if file_sha256(path) != str(sha):
            out["changed"].append(str(name))
    out["behind"] = bool(out["changed"] or out["missing_local"])
    return out


def client_status(engine: str | None = None, force: bool = False) -> dict:
    """Is this CLIENT copy behind the repo?  Everything the panel needs."""
    return client_manifest_drift(client_manifest(engine, force=force))


def latest_portable_tag() -> str:
    """The tag `releases/latest` points at, or "".

    A redirect the updater can read for free -- no API call and no token: the
    release page answers with `Location: .../releases/tag/portable-<engine>`.
    """
    url = f"https://github.com/{update_repo()}/releases/latest"
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:       # noqa: S310
            final = r.geturl() or ""
    except Exception as e:                                       # noqa: BLE001
        log(f"latest release: cannot read the redirect "
            f"({type(e).__name__}: {e})")
        return ""
    return final.rstrip("/").rsplit("/", 1)[-1]


def save_pending_update(info: dict) -> None:
    """Remember a detected-but-unapplied update, so a restart still knows."""
    try:
        PENDING_UPDATE.parent.mkdir(parents=True, exist_ok=True)
        PENDING_UPDATE.write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    except Exception as e:                                       # noqa: BLE001
        log(f"pending update: cannot write ({type(e).__name__}: {e})")


def load_pending_update() -> dict | None:
    try:
        info = json.loads(PENDING_UPDATE.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def clear_pending_update() -> None:
    try:
        PENDING_UPDATE.unlink()
    except Exception:
        pass


def release_asset_sha256(tag: str, name: str) -> str:
    """GitHub's own sha256 for one release asset, or "" when it is not there.

    A first check only: after unpacking, the core is hashed again against the
    sha256 the package's own engine-src/state.json names.
    """
    url = f"https://api.github.com/repos/{update_repo()}/releases/tags/{tag}"
    try:
        rel = json.loads(http_bytes(url).decode("utf-8"))
    except Exception as e:                                       # noqa: BLE001
        log(f"release {tag}: cannot read asset digests ({type(e).__name__}: {e})")
        return ""
    for asset in (rel.get("assets") or []):
        if str(asset.get("name") or "") == name:
            digest = str(asset.get("digest") or "")
            if digest.startswith("sha256:"):
                return digest.split(":", 1)[1].strip().lower()
    return ""


def download_portable(engine: str) -> pathlib.Path:
    """Fetch the whole portable package for `engine`: client + core together."""
    engine = str(engine or "").strip()
    if not engine:
        raise RuntimeError("download_portable needs an engine id")
    name = PORTABLE_ASSET.format(engine=engine)
    url = portable_asset_url(engine)
    want = release_asset_sha256(f"portable-{engine}", name)
    log(f"downloading {url}")
    data = http_bytes(url)
    got = sha256(data)
    if want and got != want:
        raise RuntimeError(f"downloaded portable sha256 mismatch: {got} != {want}")
    if not want:
        log("note: the release carried no asset digest; the contents decide")
    PORTABLE_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    dest = PORTABLE_UPDATE_DIR / name
    tmp = dest.with_suffix(".zip.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    log(f"downloaded portable {engine} sha256={got[:12]} bytes={len(data)}")
    return dest


def latest_portable() -> pathlib.Path | None:
    """The newest downloaded package, for `--install-portable` with no path."""
    if not PORTABLE_UPDATE_DIR.is_dir():
        return None
    hits = sorted(PORTABLE_UPDATE_DIR.glob("wfsim-portable-*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def install_portable(zip_path: "str | pathlib.Path | None" = None) -> tuple[bool, str]:
    """Copy a downloaded package over this folder, in place.

    In place, not by renaming folders: this process keeps running while the
    files are replaced, and a rename would depend on it having exited first.
    Python re-reads .py sources at import time, so the new client takes effect
    on the relaunch that follows.  Locked binaries (the running python.exe, a
    core in use) are skipped and reported instead of failing the whole update —
    they are also the files that never change.
    """
    path = pathlib.Path(zip_path) if zip_path else latest_portable()
    if path is None or not path.is_file():
        return False, "没有已下载的便携包（先跑 --download-portable <engine>）"
    stage = PORTABLE_UPDATE_DIR / "unpacked"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    root = stage.resolve()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            target = (stage / name).resolve()
            # zip-slip: an entry may never escape the staging folder
            if target != root and root not in target.parents:
                return False, f"压缩包里有越界路径：{name}"
        z.extractall(stage)
    for need in ("src/app.py", "engine/wfsim-core.exe", "engine-src/state.json"):
        if not (stage / need).is_file():
            return False, f"更新包不完整：缺少 {need}"
    try:
        pkg_state = json.loads((stage / "engine-src" / "state.json")
                               .read_text(encoding="utf-8-sig"))
    except Exception as e:                                       # noqa: BLE001
        return False, f"更新包的 state.json 读不出来：{type(e).__name__}: {e}"
    want = str(pkg_state.get("sha256") or "").strip().lower()
    got = file_sha256(stage / "engine" / "wfsim-core.exe")
    if want and got != want:
        return False, (f"更新包里的引擎 sha256 对不上：{got[:12]} != {want[:12]}"
                       "（下载不完整或被人改过）")
    copied, skipped = 0, []
    for src in sorted(stage.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(stage).as_posix()
        if rel in PORTABLE_KEEP or rel.startswith(PORTABLE_KEEP_PREFIX):
            continue
        dest = ROOT / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
        except PermissionError:
            skipped.append(rel)                 # python.exe / 引擎 / dll 正在用
        except OSError as e:                                     # noqa: PERF203
            skipped.append(f"{rel}({type(e).__name__})")
    shutil.rmtree(stage, ignore_errors=True)
    msg = f"整包更新：替换 {copied} 个文件"
    if skipped:
        msg += f"，{len(skipped)} 个被占用保持原样（下次重开会再更新）"
        log("skipped (in use): " + ", ".join(skipped[:8]))
    log(msg)
    return True, msg


# ---------------------------------------------------------------------------
# THE LOCAL LINE, MSVC FLAVOUR: the optional self-build dependency pack
# ---------------------------------------------------------------------------
#: The pack the user unpacks into the portable folder (ci/deps/README-自编译依赖.md).
#: It carries everything the MSVC build needs EXCEPT Visual Studio itself:
#: cl.exe / link.exe and the CRT + SDK import libraries are not redistributable
#: (Microsoft grants that for the runtime DLLs only), so the toolset is found on
#: the machine through vswhere instead of being shipped.
DEPS_MARKER = ROOT / "build-deps.json"
DEPS_DIR = ROOT / "build-deps"
DEPS_MSVC = ROOT / "toolchain-msvc"
#: The CI-built x86-64-v3 core: what the download line installs, always present.
GENERIC_CORE = ENGINE / "wfsim-core.exe"
#: The core this machine built for itself.  An OVERLAY, never a replacement:
#: `native_active()` decides whether it may be run at all.
NATIVE_CORE = ENGINE / "wfsim-core.native.exe"
DEFAULT_ENGINE_REL = "engine\\wfsim-core.exe"
MSVC_TARGET = "x86_64-pc-windows-msvc"
VS_BUILD_TOOLS_2022 = (r"C:\Program Files (x86)\Microsoft Visual Studio\2022"
                       r"\BuildTools")


def deps_pack() -> dict | None:
    """The unpacked self-build dependency pack, or None.

    The marker alone is not enough: a half-unpacked pack -- or one whose
    `toolchain-msvc/` the user deleted -- must read as "no pack", because every
    caller decides between "offer a build button" and "do not" on this.
    """
    try:
        info = json.loads(DEPS_MARKER.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    if not (DEPS_MSVC / "bin" / "cargo.exe").exists():
        return None
    if not (DEPS_DIR / "cargo").is_dir():
        return None
    return info


def ensure_patches() -> None:
    """Make `engine-src/patches/` exist when the pack shipped them under `ci/`.

    The dependency pack carries the patched source tree, so a build needs no
    patch step -- but `patches_sha()` (and therefore the revision bookkeeping and
    a later local-line sync) is computed from `engine-src/patches/`, which the
    pack's `ci/patches/` is the master copy of.
    """
    master = ROOT / "ci" / "patches"
    if not master.is_dir():
        return
    if any(PATCHES.glob("*.patch")):
        return
    PATCHES.mkdir(parents=True, exist_ok=True)
    for p in sorted(master.glob("*.patch")):
        shutil.copy2(p, PATCHES / p.name)
    log(f"staged {len(list(PATCHES.glob('*.patch')))} patches into {PATCHES}")


def vs_bin(prefer: str = "") -> pathlib.Path | None:
    """`.../VC/Tools/MSVC/<ver>/bin/Hostx64/x64` of the newest toolset, or None.

    Found with vswhere, so the user never sets an environment variable: any
    Visual Studio 2026 (or 2022 and newer) with the C++ workload will do.
    `WFSIM_VS_BIN` or the `vs_toolset` config key pins a specific one.
    """
    want = (str(prefer or "").strip()
            or str(load_config().get("vs_toolset") or "").strip()
            or str(os.environ.get("WFSIM_VS_BIN") or "").strip())
    if want:
        p = pathlib.Path(want)
        if p.name.lower() == "x64" and (p / "link.exe").exists():
            return p
        hits = [h for h in sorted((p / "VC" / "Tools" / "MSVC").glob(
            "*/bin/Hostx64/x64")) if (h / "link.exe").exists()]
        return hits[-1] if hits else None
    installs: list[pathlib.Path] = []
    vswhere = (pathlib.Path(os.environ.get("ProgramFiles(x86)",
                                           r"C:\Program Files (x86)"))
               / "Microsoft Visual Studio" / "Installer" / "vswhere.exe")
    if vswhere.exists():
        try:
            res = subprocess.run(
                [str(vswhere), "-all", "-prerelease", "-products", "*",
                 "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                 "-property", "installationPath"],
                capture_output=True, text=True, timeout=90)
            installs = [pathlib.Path(l.strip())
                        for l in res.stdout.splitlines() if l.strip()]
        except Exception as e:                                  # noqa: BLE001
            log(f"  vswhere failed: {type(e).__name__}: {e}")
    installs.append(pathlib.Path(VS_BUILD_TOOLS_2022))
    cands: list[tuple[tuple[int, ...], pathlib.Path]] = []
    for inst in installs:
        root = inst / "VC" / "Tools" / "MSVC"
        if not root.is_dir():
            continue
        for d in root.iterdir():
            b = d / "bin" / "Hostx64" / "x64"
            if (b / "link.exe").exists():
                key = tuple(int(x) if x.isdigit() else 0
                            for x in d.name.split("."))
                cands.append((key, b))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[-1][1]


def ascii_scratch() -> pathlib.Path:
    """A build scratch directory whose path is PLAIN ASCII.

    WHY: `cl.exe` parses part of its command line with the ANSI code page, so a
    `CARGO_HOME` under `D:\\工程\\...` makes every include lookup fail with
    `C1083: cannot open include file`.  The engine tree itself may live
    anywhere -- only these directories are staged out to `%PUBLIC%\\wfclient-build`.
    """
    for base in (os.environ.get("PUBLIC"), r"C:\Users\Public",
                 os.environ.get("ProgramData"), r"C:\ProgramData"):
        if not base:
            continue
        p = pathlib.Path(base) / "wfclient-build"
        if all(ord(c) < 128 for c in str(p)):
            return p
    raise RuntimeError("no ASCII scratch directory: set PUBLIC to an ASCII path")


def _native_profile(scratch: pathlib.Path) -> pathlib.Path | None:
    """The PGO profile to compile with, or None (a plain native build).

    The pack ships the profile of the revision it was built for.  A profile that
    describes a DIFFERENT tree is worse than none -- the compiler would optimise
    for the wrong workload -- so it is used only while the pack's `patches_sha`
    still matches this tree's.
    """
    prof = DEPS_DIR / "pgo" / "default.profdata"
    if not prof.exists():
        return None
    want = str((deps_pack() or {}).get("patches_sha") or "")
    now = patches_sha()
    if want and want != now:
        log(f"PGO profile is for patches {want[:12]}, this tree is {now[:12]}:"
            " not using it")
        return None
    return prof


def _stage_ascii(scratch: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """`CARGO_HOME` and `CARGO_TARGET_DIR` somewhere ASCII (see ascii_scratch).

    The crate registry is copied once (25 MB); the target directory is kept, so
    a rebuild after an upstream change still starts from the compiled deps.
    """
    cargo_home = scratch / "cargo"
    src = DEPS_DIR / "cargo" / "registry"
    if src.is_dir() and not (cargo_home / "registry").exists():
        cargo_home.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, cargo_home / "registry")
        log(f"staged the offline crate registry into {cargo_home}")
    else:
        cargo_home.mkdir(parents=True, exist_ok=True)
    target = scratch / "target"
    (scratch / "tmp").mkdir(parents=True, exist_ok=True)
    (scratch / "home").mkdir(parents=True, exist_ok=True)
    return cargo_home, target


def _run_cargo_native(vs: pathlib.Path, scratch: pathlib.Path,
                      profile: pathlib.Path | None,
                      *, generate: str = "", features: str = "") -> pathlib.Path:
    """One MSVC cargo build.  Streamed, because a failed build must say why."""
    cargo = DEPS_MSVC / "bin" / "cargo.exe"
    if not cargo.exists():
        raise RuntimeError(f"missing the pack's cargo: {cargo}")
    cargo_home, target = _stage_ascii(scratch)
    flags = "-C target-cpu=native -C target-feature=+crt-static"
    if generate:
        flags += f" -Cprofile-generate={generate}"
    elif profile is not None:
        flags += f" -Cprofile-use={profile}"
    env = dict(os.environ)
    env.update({
        "PATH": os.pathsep.join([str(vs), str(DEPS_MSVC / "bin"),
                                 env.get("SystemRoot", r"C:\Windows") + r"\System32"]),
        "CARGO_HOME": str(cargo_home),
        "CARGO_TARGET_DIR": str(target),
        "RUSTUP_HOME": str(DEPS_MSVC),
        "TEMP": str(scratch / "tmp"), "TMP": str(scratch / "tmp"),
        "HOME": str(scratch / "home"), "USERPROFILE": str(scratch / "home"),
        "RUSTFLAGS": flags,
        # The pack carries the crates; a build must never need crates.io.
        "CARGO_NET_OFFLINE": "true",
    })
    cmd = [str(cargo), "build", "--profile", "maxperf", "--target", MSVC_TARGET,
           "-p", "wfsim-core"]
    if features:
        cmd += ["--features", features]
    log("building: " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=WORK, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    lines: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        print(line, flush=True)
        lines.append(line)
    rc = proc.wait()
    if rc != 0:
        for line in lines[-25:]:
            log("  cargo| " + line)
        raise RuntimeError("cargo build failed with exit code {}:\n{}".format(
            rc, "\n".join(lines[-25:])))
    out = target / MSVC_TARGET / "maxperf" / "wfsim-core.exe"
    if not out.exists():
        raise RuntimeError(f"built binary not found at {out}")
    return out


def _train_profile(scratch: pathlib.Path, gen_exe: pathlib.Path) -> pathlib.Path | None:
    """Sampling run: drive the frozen corpus with the instrumented core, merge.

    Only used when the pack carries no profile for this tree -- the shipped one
    saves this two-and-a-half minute detour.  Nothing here is required for a
    correct build; a failure just means "compile without a profile".
    """
    trainer = ROOT / "ci" / "tools" / "pgo_train.py"
    llvm_profdata = (DEPS_MSVC / "lib" / "rustlib" / MSVC_TARGET / "bin"
                     / "llvm-profdata.exe")
    if not trainer.exists() or not llvm_profdata.exists():
        log("no PGO trainer or llvm-profdata in the pack: building without a profile")
        return None
    prof = scratch / "pgo-gen"
    shutil.rmtree(prof, ignore_errors=True)
    prof.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, str(trainer), str(gen_exe), "--board", "40",
           "--riven", "1", "--prof", str(prof), "--port", "9500"]
    log("training a PGO profile on this machine (about two minutes)")
    try:
        run = subprocess.run(cmd, cwd=str(trainer.parent), env=env,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=3600)
    except Exception as e:                                      # noqa: BLE001
        log(f"PGO training failed to run: {type(e).__name__}: {e}")
        return None
    for line in (run.stdout or "").splitlines()[-6:]:
        log("  pgo| " + line)
    raw = sorted(prof.glob("*.profraw"))
    if not raw:
        log("PGO sampling produced no .profraw: building without a profile")
        return None
    out = scratch / "native.profdata"
    merge = subprocess.run([str(llvm_profdata), "merge", "-o", str(out)]
                           + [str(r) for r in raw],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    if merge.returncode != 0 or not out.exists():
        log("llvm-profdata merge failed: building without a profile")
        return None
    log(f"PGO profile trained on this machine ({len(raw)} run(s)): {out}")
    return out


def build_native_core(clean: bool = False,
                      allow_train: bool = True) -> tuple[pathlib.Path, bool]:
    """Build the fastest engine this machine can run, from the dependency pack.

    Returns `(exe, used_pgo)`.  Nothing here touches the network and nothing here
    replaces the live core: `install_native_core` adopts the result as the
    overlay after `verify_native_core` has passed.
    """
    info = deps_pack()
    if info is None:
        raise RuntimeError("self-build dependency pack not found: unpack "
                           "wfclient-deps-<engine>.zip into this folder first")
    vs = vs_bin()
    if vs is None:
        raise RuntimeError("no MSVC toolset found: install Visual Studio 2026 "
                           "(2022 or newer works too) with the C++ workload, or "
                           "set WFSIM_VS_BIN to ...\\VC\\Tools\\MSVC\\<ver>\\bin\\Hostx64\\x64")
    log(f"MSVC toolset: {vs}")
    ensure_patches()
    scratch = ascii_scratch()
    scratch.mkdir(parents=True, exist_ok=True)
    if clean:
        shutil.rmtree(scratch / "target", ignore_errors=True)
        log("cleaned the MSVC target directory for a fresh build")
    profile = _native_profile(scratch)
    if profile is None and allow_train:
        gen = _run_cargo_native(vs, scratch, None,
                                generate=str(scratch / "pgo-gen"),
                                features="wfsim-core/fast-alloc,wfsim-core/pgo-exit")
        profile = _train_profile(scratch, gen)
        if profile is None:
            return _run_cargo_native(vs, scratch, None,
                                     features="wfsim-core/fast-alloc"), False
    used_pgo = profile is not None
    exe = _run_cargo_native(vs, scratch, profile, features="wfsim-core/fast-alloc")
    log(f"native engine built: {exe} pgo={used_pgo}")
    return exe, used_pgo


def verify_native_core(exe: pathlib.Path) -> None:
    """The five site anchors against the freshly built core, in seconds.

    A locally built core is unchecked arithmetic until this passes: the same five
    reference rows every release engine has to reproduce exactly (score + work,
    computed by the site's own wasm).
    """
    tools = ROOT / "ci" / "tools"
    runner = tools / "run_corpus.py"
    if not runner.exists() or not (tools / "verify_pipeline.py").exists():
        log("this pack carries no frozen corpus: skipping the local anchor check")
        return
    corpus = tools / "empty.json"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-B", str(runner), str(exe),
           "--corpus", str(corpus), "--anchors", "--port", "9540"]
    log("verifying the new core against the five site anchors")
    run = subprocess.run(cmd, cwd=str(tools), env=env, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=900)
    out = run.stdout or ""
    for line in out.strip().splitlines()[-3:]:
        log("  gate| " + line)
    if run.returncode != 0 or "PASS" not in out:
        raise RuntimeError("the freshly built core failed the bit-identity gate:\n"
                           + "\n".join(out.strip().splitlines()[-12:]))


def native_active() -> bool:
    """May the locally built core be run INSTEAD of the generic one?

    Only while it was built from the revision this client is on: the build
    records the engine id it was made for, and the update line rewrites
    `state['engine']` whenever it installs a new generic core.  A mismatch means
    the sources this machine built from are not what the server serves any more,
    so the overlay is ignored and the generic core runs -- the fallback can never
    be lost to a local experiment.

    Memoised on (mtime, size) of the overlay + state.json: this is called from
    the fleet's engine path, and hashing 15 MB per call is not free.
    """
    global _NATIVE_MEMO
    try:
        st = NATIVE_CORE.stat()
        state_m = STATE.stat().st_mtime_ns if STATE.exists() else 0
    except OSError:
        return False
    stamp = (st.st_mtime_ns, st.st_size, state_m)
    if _NATIVE_MEMO is not None and _NATIVE_MEMO[0] == stamp:
        return _NATIVE_MEMO[1]
    ok = False
    state = load_state()
    nat = state.get("native") or {}
    if (isinstance(nat, dict) and nat.get("engine")
            and str(nat.get("engine")) == str(state.get("engine") or "")
            and str(nat.get("patches_sha") or "") == patches_sha()
            and str(nat.get("sha256") or "").lower() == file_sha256(NATIVE_CORE)
            and str(nat.get("cpu") or "") in ("", cores.cpu_model())):
        ok = True
    _NATIVE_MEMO = (stamp, ok)
    return ok


_NATIVE_MEMO: tuple[tuple, bool] | None = None


def native_ignore_reason() -> str:
    """Why the local native core is NOT in force: "", "missing", "revision", "cpu".

    `-C target-cpu=native` produces a machine-specific binary, so the one thing
    that must never happen is running it on another CPU: the build records the
    CPU it was made on, and a recorded mismatch refuses the overlay exactly like
    a revision mismatch does.  The whole portable folder can be copied to a
    second PC, so this is checked wherever the engine path is taken, not only at
    startup.

    "" means it IS in force (or there is no overlay to talk about), so callers
    can show a specific sentence: "the server moved on" and "this was built for
    another CPU" need different advice.
    """
    try:
        if not NATIVE_CORE.exists():
            return "missing"
        state = load_state()
        nat = state.get("native") or {}
        if not isinstance(nat, dict) or not nat.get("engine"):
            return "missing"
        if str(nat.get("engine")) != str(state.get("engine") or ""):
            return "revision"
        built_for = str(nat.get("cpu") or "")
        if built_for and built_for != cores.cpu_model():
            return "cpu"
    except Exception:
        return "missing"
    return ""


def engine_exe_override(configured: str) -> str | None:
    """The local native core to run instead of the configured one, or None.

    Only for the default engine name: a user who pointed `engine_exe` at their
    own binary gets exactly that binary.
    """
    if str(configured or "").replace("/", "\\").strip() != DEFAULT_ENGINE_REL:
        return None
    if not native_active():
        return None
    return str(NATIVE_CORE)


def engine_path() -> pathlib.Path:
    """The core the fleet should start: the overlay when it still matches."""
    return NATIVE_CORE if native_active() else GENERIC_CORE


def engine_status() -> dict:
    """What the settings page shows: which core, and why."""
    state = load_state()
    nat = state.get("native") or {}
    ok = native_active()
    if ok:
        kind = "native"
    elif core_source() == CORE_SOURCE_PREBUILT:
        kind = "generic"
    else:
        kind = "gnu"
    return {
        "kind": kind,
        "native_ok": ok,
        "native_file": NATIVE_CORE.exists(),
        "native": nat if isinstance(nat, dict) else {},
        "native_reason": native_ignore_reason(),
        "native_cpu": str(nat.get("cpu") or "") if isinstance(nat, dict) else "",
        "this_cpu": cores.cpu_model(),
        "engine": state.get("engine"),
        "cpu": state.get("cpu"),
        "deps": deps_pack() is not None,
        "generic": GENERIC_CORE.exists(),
    }


def install_native_core(built: pathlib.Path, *, pgo: bool) -> pathlib.Path:
    """Adopt a locally built core as the OVERLAY; the generic core stays put.

    `engine/wfsim-core.exe` remains the CI-built core the download line installs,
    so a native core that stops matching the served revision is simply ignored
    (`native_active`) instead of leaving the machine on a stale binary.
    """
    built = pathlib.Path(built)
    if not built.exists():
        raise RuntimeError(f"built binary not found at {built}")
    ENGINE.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE / "wfsim-core.native.new.exe"
    shutil.copy2(built, tmp)
    os.replace(tmp, NATIVE_CORE)
    digest = file_sha256(NATIVE_CORE)
    info = deps_pack() or {}
    state = load_state()
    engine = str(info.get("engine") or state.get("engine") or "")
    state["native"] = {
        "sha256": digest,
        "engine": engine,
        "release": str(info.get("release") or state.get("release") or ""),
        "commit": str(info.get("commit") or state.get("server_commit") or ""),
        "patches_sha": patches_sha(),
        "pgo": bool(pgo),
        "cpu": cores.cpu_model(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_state(state)
    if not native_active():
        # Should not happen (the fields just written are exactly what it checks),
        # but a silent overlay would be the worst possible outcome.
        raise RuntimeError("the freshly installed native core did not validate; "
                           "the generic core will keep being used")
    log(f"installed {NATIVE_CORE} engine={engine} pgo={bool(pgo)} "
        f"sha256={digest[:12]}")
    return NATIVE_CORE


def install_staged() -> pathlib.Path:
    """Install the last staged core.  NO network, NO build, NO running engine."""
    if not PENDING.exists():
        raise RuntimeError(f"no staged core metadata at {PENDING}")
    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    staged = pathlib.Path(str(pending.get("staged") or STAGED_CORE))
    if not staged.exists():
        raise RuntimeError(f"staged core not found at {staged}")
    expected = str(pending.get("sha256") or "")
    actual = file_sha256(staged)
    if expected and expected.lower() != actual.lower():
        raise RuntimeError(f"staged core checksum mismatch: {expected} != {actual}")
    target = ENGINE / "wfsim-core.exe"
    prev = ENGINE / "wfsim-core.prev.exe"
    ENGINE.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.copy2(target, prev)
    tmp = ENGINE / "wfsim-core.new.exe"
    shutil.copy2(staged, tmp)
    os.replace(tmp, target)
    cfg = load_config()
    cfg["engine_exe"] = "engine\\wfsim-core.exe"
    cfg["known_engine"] = pending.get("engine")
    cfg["client_protocol"] = CLIENT_PROTOCOL
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    state = load_state()
    state.update({
        "engine": pending.get("engine"),
        "release": pending.get("release"),
        "server_commit": pending.get("server_commit"),
        "ref": "main",
        "built": pending.get("built"),
        "installed": str(target),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "client_protocol": CLIENT_PROTOCOL,
        "optimizer_tree_sha": pending.get("optimizer_tree_sha"),
        "patches_sha": pending.get("patches_sha"),
        "cpu": cores.cpu_model(),
        "sha256": actual,
    })
    save_state(state)
    try:
        PENDING.unlink()
    except FileNotFoundError:
        pass
    log(f"installed {target} for engine {pending.get('engine')} sha256={actual[:12]}")
    return target


def install_core(built: pathlib.Path, release: dict) -> pathlib.Path:
    ENGINE.mkdir(parents=True, exist_ok=True)
    target = ENGINE / "wfsim-core.exe"
    prev = ENGINE / "wfsim-core.prev.exe"
    if target.exists():
        shutil.copy2(target, prev)
    tmp = ENGINE / "wfsim-core.new.exe"
    shutil.copy2(built, tmp)
    os.replace(tmp, target)
    cfg = load_config()
    cfg["engine_exe"] = "engine\\wfsim-core.exe"
    cfg["known_engine"] = release.get("engine")
    cfg["client_protocol"] = CLIENT_PROTOCOL
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    state = load_state()
    state.update({"cpu": cores.cpu_model()})
    save_state(state)
    log(f"installed {target} for engine {release.get('engine')}")
    return target


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="only compare release engine id")
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--build-only", action="store_true",
                    help="build and stage the core; do not replace the live exe")
    ap.add_argument("--install-staged", action="store_true",
                    help="install the previously staged core with no network/build")
    ap.add_argument("--download-core", metavar="ENGINE", default="",
                    help="prebuilt line: fetch + stage the published core for this engine id")
    ap.add_argument("--download-portable", metavar="ENGINE", default="",
                    help="prebuilt line: fetch the whole published package "
                         "(client + core) for this engine id")
    ap.add_argument("--install-portable", metavar="ZIP", nargs="?", const="", default=None,
                    help="copy the downloaded package over this folder in place "
                         "(no argument = the newest one already downloaded)")
    ap.add_argument("--build-native", action="store_true",
                    help="local line: build the fastest engine for THIS machine from the "
                         "self-build dependency pack, check it, adopt it as the overlay")
    ap.add_argument("--native-status", action="store_true",
                    help="print which core would run right now, and why")
    ap.add_argument("--no-train", action="store_true",
                    help="with --build-native: never sample a PGO profile, just build")
    ap.add_argument("--clean", action="store_true",
                    help="build from an empty build-cache/target")
    return ap


def _install_staged_cli() -> int:
    try:
        install_staged()
        return 0
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1


def _native_status_cli() -> int:
    st = engine_status()
    labels = {"native": "native（本机编译，最快）",
              "generic": "通用 x86-64-v3（AVX2）",
              "gnu": "本地编译（GNU）"}
    log(f"deps pack   : {'yes' if st['deps'] else 'no'}")
    log(f"generic core: {'yes' if st['generic'] else 'no'}  {GENERIC_CORE}")
    log(f"native core : {'yes' if st['native_file'] else 'no'}  {NATIVE_CORE}")
    nat = st["native"] or {}
    if st["native_ok"]:
        log(f"native note : active, engine {nat.get('engine')} "
            f"pgo={nat.get('pgo')} built {nat.get('built_at')}")
    elif st["native_file"]:
        log(f"native note : ignored (built for engine {nat.get('engine')}, "
            f"this client is on {st.get('engine')})")
    log(f"would run   : {labels.get(st['kind'], st['kind'])}")
    return 0


def _build_native_cli(clean: bool, allow_train: bool) -> int:
    try:
        built, pgo = build_native_core(clean=clean, allow_train=allow_train)
        verify_native_core(built)
        install_native_core(built, pgo=pgo)
        log(f"OK: native engine is now the one this machine runs "
            f"({'PGO' if pgo else 'no PGO profile'})")
        return 0
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1


def patches_sha() -> str:
    """A FINGERPRINT OF THE PATCH SET: a new or edited patch is a new engine.

    The engine id and the optimizer tree are the SERVER's signals, and neither
    moves when this client's own patches do — so without this a change to the
    fleet drive (or to any other patch) would never rebuild an install that is
    otherwise "up to date", and the new code would never reach a machine.
    """
    h = hashlib.sha1()
    for patch in sorted(PATCHES.glob("*.patch")):
        h.update(patch.name.encode("utf-8"))
        h.update(patch.read_bytes())
    return h.hexdigest()[:12]


def _update_signals(rel: dict, state: dict):
    # THREE SIGNALS.
    #   `engine`             the id the server validates and hands work to
    #   `optimizer/src` SHA  the riven search, which that id does not hash
    #   `patches` SHA        this client's OWN patches, which neither of the two
    #                        above moves for (see `patches_sha`)
    engine_changed = (state.get("engine") != rel.get("engine"))
    stored_opt = state.get("optimizer_tree_sha")
    opt_sha = None
    try:
        opt_sha = optimizer_tree_sha()
    except Exception as e:
        log(f"optimizer/src tree unavailable ({e}); comparing the engine id only")
    opt_changed = bool(opt_sha) and stored_opt != opt_sha
    cur_patches = patches_sha()
    patches_changed = state.get("patches_sha") != cur_patches
    if core_source() == CORE_SOURCE_PREBUILT:
        # A prebuilt package ships no patch set and no sources, so both of these
        # compare a local tree that does not exist: the empty patch set hashes to
        # da39a3ee… against a recorded 1e8bb8f3…, which used to read as "our
        # patches changed" and sent a portable zip off to run cargo it does not
        # have.  This line only ever follows the served engine id.
        opt_changed = False
        patches_changed = False
    hash_ok = installed_core_matches_state()
    return (engine_changed, opt_changed, patches_changed, hash_ok, stored_opt, opt_sha,
            cur_patches)


def _print_check(rel: dict, state: dict, stored_opt, opt_sha,
                 hash_ok: bool, up_to_date: bool,
                 stored_patches=None, cur_patches=None) -> int:
    log(f"engine  online={rel.get('engine')} local={state.get('engine')}")
    log(f"optimizer/src online={opt_sha} local={stored_opt}")
    log(f"patches local={cur_patches} installed={stored_patches}")
    log(f"installed core hash={'ok' if hash_ok else 'changed/missing'}")
    return 0 if up_to_date else 2


def _sync_and_patch() -> str:
    # The server's engine id is the compatibility gate.  The release commit is
    # often a generated site commit that is not pushed, so the updater always
    # reads the public main tree rather than trusting that commit.
    _, _, synced_opt = sync_sources("main")
    apply_patches()
    return synced_opt


def _build_with_retry(clean: bool) -> pathlib.Path:
    try:
        return build_core(clean=clean)
    except RuntimeError as e:
        # ONE CLEAN RETRY.  If the failure was a wrecked incremental artefact
        # this fixes it; a real compile error comes back and the caller falls
        # back to the old core.
        log(f"first build failed, retrying from a clean target: {e}")
        return build_core(clean=True)


def _save_install_state(state: dict, rel: dict, built: pathlib.Path,
                        synced_opt: str) -> None:
    state.update({
        "engine": rel.get("engine"),
        "release": rel.get("release"),
        "server_commit": rel.get("commit"),
        "ref": "main",
        "built": str(built),
        "installed": str(ENGINE / "wfsim-core.exe"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "client_protocol": CLIENT_PROTOCOL,
        "optimizer_tree_sha": synced_opt,
        "patches_sha": patches_sha(),
    })
    save_state(state)


def main() -> int:
    args = _build_parser().parse_args()
    if args.install_staged:
        return _install_staged_cli()
    if args.native_status:
        return _native_status_cli()
    if args.build_native:
        # The local line: offline, self-contained, and it never replaces the
        # generic core (see install_native_core).
        return _build_native_cli(clean=args.clean, allow_train=not args.no_train)
    if args.download_core:
        try:
            download_core(args.download_core)
            return 0
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            return 1
    if args.download_portable:
        try:
            download_portable(args.download_portable)
            return 0
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            return 1
    if args.install_portable is not None:
        # install_portable() logs its own summary line (including the skipped
        # list); logging the returned message again printed the same line twice.
        ok, _msg = install_portable(args.install_portable or None)
        return 0 if ok else 1

    try:
        rel = release_info()
        state = load_state()
        target = ENGINE / "wfsim-core.exe"
        (engine_changed, opt_changed, patches_changed, hash_ok, stored_opt, opt_sha,
         cur_patches) = _update_signals(rel, state)
        up_to_date = (not engine_changed and not opt_changed and not patches_changed
                      and target.exists() and hash_ok)
        if args.check:
            return _print_check(rel, state, stored_opt, opt_sha, hash_ok, up_to_date,
                                state.get("patches_sha"), cur_patches)
        if up_to_date and not args.force:
            log(f"engine {rel.get('engine')} with optimizer {opt_sha} is already installed")
            return 0
        log(f"update needed: engine {state.get('engine')} -> {rel.get('engine')}, "
            f"optimizer/src {stored_opt} -> {opt_sha}, "
            f"patches {state.get('patches_sha')} -> {cur_patches}")
        synced_opt = _sync_and_patch()
        if args.fetch_only:
            return 0
        built = _build_with_retry(args.clean)
        if args.build_only:
            stage_core(built, rel, synced_opt)
            return 0
        if not args.no_build:
            install_core(built, rel)
        _save_install_state(state, rel, built, synced_opt)
        return 0
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
