# -*- coding: utf-8 -*-
"""The two update lines, and the buttons that only exist on one of them.

A package that comes off the release page is built by CI elsewhere and shipped
whole: it has no `toolchain/`, no `patches/` and no `core-host-template/`, so it
can never compile a core.  It used to behave as if it could — the empty patch
set hashed to `da39a3ee…` against the recorded `1e8bb8f3…`, that read as "our
patches changed", and the client paused its fleet every five minutes to run a
cargo that is not in the zip (measured 2026-10-17, on the first CI zip).

This pins the mechanism:

  1. `sync_engine.core_source()` — the state key, the legacy `prebuilt` flag, and
     what is actually on disk;
  2. `_update_signals()` ignores the local optimizer/patch signals on that line,
     and still honours them on the source line;
  3. `download_core()` refuses bytes whose sha256 is not the published one;
  4. the settings screen does not offer the two build actions on a prebuilt
     package — HIDDEN, not shown-disabled.

Run with the client's own python (the screen needs textual):
    python test_core_lines.py [客户端目录]  < /dev/null
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                    pathlib.Path(__file__).resolve().parent / "client-portable-20261017-0345-ci")
ROOT = ROOT.resolve()
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

import app as appmod                        # noqa: E402
import sync_engine as se                    # noqa: E402
from textual.app import App                 # noqa: E402
from textual.widgets import Static          # noqa: E402

ok = bad = 0
def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1; print("  PASS  %s" % name)
    else:
        bad += 1; print("  FAIL  %s  %s" % (name, extra))


class Swap:
    """Temporarily replace module attributes, whatever happens."""
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}
    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k, _MISSING)
            setattr(self.mod, k, v)
        return self
    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is _MISSING:
                delattr(self.mod, k)
            else:
                setattr(self.mod, k, v)
        return False


_MISSING = object()


class Cargo:
    """Stands in for `<toolchain>/rust/bin/cargo.exe`."""
    def __init__(self, there): self.there = there
    def exists(self): return self.there


class Patches:
    def __init__(self, there): self.there = there
    def is_dir(self): return self.there


def source_for(state, cargo, patches):
    with Swap(se, load_state=lambda: dict(state),
              RUST=_FakeRoot(cargo), PATCHES=Patches(patches)):
        return se.core_source()


class _FakeRoot:
    """`RUST / "bin" / "cargo.exe"` — only that expression is used."""
    def __init__(self, cargo): self.cargo = cargo
    def __truediv__(self, other):
        if other == "bin":
            return _FakeBin(self.cargo)
        return pathlib.Path(other)


class _FakeBin:
    def __init__(self, cargo): self.cargo = cargo
    def __truediv__(self, other):
        return Cargo(self.cargo) if other == "cargo.exe" else pathlib.Path(other)


# ---- 1. which line is this copy on? ---------------------------------------
P, L = se.CORE_SOURCE_PREBUILT, se.CORE_SOURCE_LOCAL
cases = [
    ("the state says prebuilt",            {"core_source": P},        False, False, P),
    ("the state says local, and wins",     {"core_source": L},        False, False, L),
    ("a state that predates the key",      {"core_source": L},        True,  True,  L),
    ("the legacy prebuilt flag",           {"prebuilt": True},        True,  True,  P),
    ("no cargo on disk",                   {},                       False, True,  P),
    ("no patch set on disk",               {},                       True,  False, P),
    ("cargo and patches both there",       {},                       True,  True,  L),
    ("a junk state value falls through",   {"core_source": "??"},     False, True,  P),
]
for label, state, cargo, patches, want in cases:
    got = source_for(state, cargo, patches)
    check("%s -> %s" % (label, want), got == want, "got %r" % got)

# ---- 2. the prebuilt line ignores the local signals ------------------------
REAL = {"optimizer_tree_sha", "patches_sha", "optimizer_tree_sha", "load_state",
        "installed_core_matches_state", "core_source"}
state = {"engine": "old", "optimizer_tree_sha": "stored-opt",
         "patches_sha": "1e8bb8f3b4f4"}
common = dict(load_state=lambda: dict(state),
              optimizer_tree_sha=lambda: "online-opt",
              patches_sha=lambda: "da39a3ee5e6b4b0d3255bfef95601890afd80709",
              installed_core_matches_state=lambda: True)
forced = dict(common, core_source=lambda: P)
with Swap(se, **forced):
    engine_changed, opt_changed, patches_changed, *_ = se._update_signals(
        {"engine": "new"}, dict(state))
check("engine id moving still counts on the prebuilt line", engine_changed is True)
check("the optimizer tree is not compared on the prebuilt line",
      opt_changed is False, opt_changed)
check("an empty patch set is not 'our patches changed' on the prebuilt line",
      patches_changed is False, patches_changed)

with Swap(se, **dict(common, core_source=lambda: L)):
    engine_changed, opt_changed, patches_changed, *_ = se._update_signals(
        {"engine": "new"}, dict(state))
check("on the source line the same inputs DO count",
      (opt_changed, patches_changed) == (True, True),
      (opt_changed, patches_changed))

# ---- 3. a download is only accepted as the published bytes -----------------
GOOD = "a" * 64
STAGE = ROOT / "bench" / "corelines-staged"
calls = []


def fake_http(identity: bytes, exe: bytes):
    def _get(url, *a, **k):
        return identity if url.endswith(".json") else exe
    return _get


IDENT = ('{"sha256": "%s", "release_online": "rel-1", "commit": "c1"}' % GOOD).encode("utf-8")

with Swap(se, update_repo=lambda: "owner/repo",
          http_bytes=fake_http(IDENT, b"the published core"),
          stage_core=lambda built, rel, opt: calls.append((built, rel, opt)),
          STAGED_DIR=STAGE, STAGED_CORE=STAGE / "wfsim-core.exe",
          sha256=lambda data: GOOD):
    out = se.download_core("abc123")
    check("a matching download is staged",
          len(calls) == 1 and str(out).endswith("wfsim-core.exe"), (len(calls), out))
    check("the staged record keeps the served release",
          calls and calls[0][1]["engine"] == "abc123" and calls[0][1]["release"] == "rel-1",
          calls[0][1] if calls else None)
    check("the download landed next to the staged core",
          (STAGE / "downloaded.exe").exists() and
          (STAGE / "downloaded.exe").read_bytes() == b"the published core")
    check("no .tmp is left behind", not (STAGE / "downloaded.tmp").exists())

with Swap(se, update_repo=lambda: "owner/repo",
          http_bytes=fake_http(IDENT, b"tampered bytes"),
          sha256=lambda data: "b" * 64,
          STAGED_DIR=STAGE, STAGED_CORE=STAGE / "wfsim-core.exe"):
    try:
        se.download_core("abc123")
        raised = None
    except RuntimeError as e:
        raised = str(e)
    check("bytes that do not match the published sha256 are refused",
          raised is not None and "sha256 mismatch" in raised, raised)
    check("a refused download is not staged",
          not any(c[1].get("engine") == "abc123" for c in calls[1:]), calls)

with Swap(se, update_repo=lambda: "owner/repo",
          http_bytes=fake_http(b'{"release_online": "rel-1"}', b"x"),
          STAGED_DIR=STAGE, STAGED_CORE=STAGE / "wfsim-core.exe"):
    try:
        se.download_core("abc123")
        raised = None
    except RuntimeError as e:
        raised = str(e)
    check("an identity file with no sha256 is refused",
          raised is not None and "no sha256" in raised, raised)

try:
    se.download_core("")
    raised = None
except RuntimeError as e:
    raised = str(e)
check("an empty engine id is refused", raised is not None and "needs an engine id" in raised,
      raised)

# ---- 4. the settings screen does not offer what cannot be done -------------
class FakeFleet:
    def __init__(self):
        self.cfg = {}
        self.wire_engine = "85363d8320d9"
        self.engine_id = "85363d8320d9"
        self.maintenance = False
        self._proto8_off = False
    def slot_count(self):
        return 3


class Host(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen
    def on_mount(self):
        self.push_screen(self._screen)


async def screen_for(line):
    old = appmod.core_source
    appmod.core_source = (lambda: line)
    try:
        scr = appmod.SettingsScreen(FakeFleet())
        app = Host(scr)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ids = {b.id for b in scr.query("Button")}
            note = scr.query_one("#settingsnote", Static).render().plain
        return ids, note
    finally:
        appmod.core_source = old


ids_pre, note_pre = asyncio.run(screen_for(P))
ids_loc, note_loc = asyncio.run(screen_for(L))
check("a prebuilt package hides 清理缓存重建引擎",
      "rebuild_engine" not in ids_pre, sorted(ids_pre))
check("a prebuilt package hides 快速重新编译引擎",
      "rebuild_fast" not in ids_pre, sorted(ids_pre))
check("a prebuilt package still shows the settings it CAN honour",
      {"engine_minus", "slots_plus", "log_level_toggle", "scale125"} <= ids_pre,
      sorted(ids_pre))
check("a source package still shows both build actions",
      {"rebuild_engine", "rebuild_fast"} <= ids_loc, sorted(ids_loc))
check("the notes say what a prebuilt package does instead of compiling",
      "本包是预编译包" in note_pre and "自动下载" in note_pre, note_pre[:120])
check("the notes explain the two build actions on the source line",
      "清理重建会暂停接任务" in note_loc, note_loc[:120])

app_src = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
check("app.py: the choice is made in __init__, from core_source()",
      "self.prebuilt = core_source() == sync_engine.CORE_SOURCE_PREBUILT" in app_src)
check("app.py: _refresh does not touch buttons that are not there",
      "if not self.prebuilt:\n                self.query_one(\"#rebuild_engine\", Button).disabled = busy"
      in app_src)

print("\n%d passed, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)
