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


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def configured_pat() -> str:
    """The user-controlled GitHub PAT.  Never log or return it for display."""
    return str(load_config().get("github_pat") or "").strip()


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
