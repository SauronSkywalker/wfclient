# -*- coding: utf-8 -*-
"""THE LOCAL LINE, MSVC FLAVOUR: the dependency pack, and the overlay core.

The delivered portable package ships a CI-built generic core and, optionally, a
user-unpacked dependency pack (everything the MSVC build needs EXCEPT Visual
Studio).  This pins the mechanism that decides whether a machine may compile for
itself, and — the part that must never break — that a locally compiled core can
never become the only core on disk:

  1. `deps_pack()` is a real readiness test, not "a marker file exists";
  2. `native_active()` runs the overlay ONLY for the revision it was built for
     (engine id + patch set + its own sha256), so an upstream move falls back;
  3. `engine_exe_override()` keeps a user-set `engine_exe` untouched;
  4. `install_native_core()` writes `wfsim-core.native.exe` and leaves
     `wfsim-core.exe` byte-identical;
  5. the settings screen grows the third build action exactly when the pack is
     there, and says which core is running;
  6. `--build-native` is offline by construction (`CARGO_NET_OFFLINE`).

Run with the client's own python (the screen needs textual):
    python test_native_line.py [客户端目录]  < /dev/null
"""
import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile

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
    _MISSING = object()
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}
    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k, self._MISSING)
            setattr(self.mod, k, v)
        return self
    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is self._MISSING:
                delattr(self.mod, k)
            else:
                setattr(self.mod, k, v)
        return False


TMP = pathlib.Path(tempfile.mkdtemp(prefix="wfsim-native-"))
ENGINE = TMP / "engine"
ENGINE.mkdir(parents=True)
GENERIC = ENGINE / "wfsim-core.exe"
GENERIC.write_bytes(b"GENERIC-CORE" * 100)
NATIVE = ENGINE / "wfsim-core.native.exe"
STATE = TMP / "engine-src" / "state.json"
STATE.parent.mkdir(parents=True)
PATCHES = TMP / "engine-src" / "patches"
PATCHES.mkdir(parents=True)
(PATCHES / "a.patch").write_text("patched\n", encoding="utf-8")
DEPS = TMP / "build-deps"
DEPS_MSVC = TMP / "toolchain-msvc"
MARKER = TMP / "build-deps.json"
CI = TMP / "ci"
(CI / "patches").mkdir(parents=True)
(CI / "patches" / "a.patch").write_text("patched\n", encoding="utf-8")


def write_state(**over):
    base = {"engine": "85363d8320d9", "release": "5d64e09f6cd9",
            "core_source": "prebuilt", "sha256": "x" * 64, "native": None}
    base.update(over)
    STATE.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")


def make_pack():
    (DEPS_MSVC / "bin").mkdir(parents=True, exist_ok=True)
    (DEPS_MSVC / "bin" / "cargo.exe").write_bytes(b"MZ")
    (DEPS / "cargo").mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps({"engine": "85363d8320d9", "schema": 1}),
                      encoding="utf-8")


S = dict(DEPS_MARKER=MARKER, DEPS_DIR=DEPS, DEPS_MSVC=DEPS_MSVC, ENGINE=ENGINE,
         GENERIC_CORE=GENERIC, NATIVE_CORE=NATIVE, STATE=STATE, PATCHES=PATCHES)

# ---- 1. the dependency pack has to be USABLE, not merely present ----------
with Swap(se, **S):
    check("no marker file -> no pack", se.deps_pack() is None)
    MARKER.write_text("{ not json", encoding="utf-8")
    check("a broken marker is not a pack", se.deps_pack() is None)
    MARKER.write_text(json.dumps({"engine": "e"}), encoding="utf-8")
    check("a marker with nothing unpacked is not a pack", se.deps_pack() is None)
    make_pack()
    pack = se.deps_pack()
    check("a marker with the toolchain and the crates is a pack",
          isinstance(pack, dict) and pack.get("engine") == "85363d8320d9", pack)
    shutil.rmtree(DEPS / "cargo")
    check("deleting the crates turns the pack off again", se.deps_pack() is None)
    (DEPS / "cargo").mkdir(parents=True, exist_ok=True)

# ---- 2. the overlay only runs for the revision it was built for -----------
built = TMP / "built.exe"
built.write_bytes(b"MY-NATIVE-CORE" * 100)
NATIVE.write_bytes(built.read_bytes())
with Swap(se, **S):
    # patches_sha() reads PATCHES, so it has to be taken INSIDE the swap.
    patches_now = se.patches_sha()
    nat_ok = {"sha256": se.file_sha256(NATIVE), "engine": "85363d8320d9",
              "patches_sha": patches_now, "pgo": True}
    write_state(**{"native": dict(nat_ok)})
    se._NATIVE_MEMO = None
    check("matching engine + patches + sha -> the overlay runs", se.native_active() is True)
    se._NATIVE_MEMO = None
    write_state(**{"engine": "deadbeefcafe", "native": dict(nat_ok)})
    check("a moved server engine id -> the generic core runs again",
          se.native_active() is False)
    se._NATIVE_MEMO = None
    write_state(**{"native": dict(nat_ok, sha256="0" * 64)})
    check("a native file whose bytes changed -> ignored", se.native_active() is False)
    se._NATIVE_MEMO = None
    write_state(**{"native": dict(nat_ok, patches_sha="000000000000")})
    check("a native build from another patch set -> ignored", se.native_active() is False)
    se._NATIVE_MEMO = None
    write_state(**{"native": None})
    check("no recorded native build -> ignored", se.native_active() is False)
    se._NATIVE_MEMO = None
    write_state(**{"native": dict(nat_ok)})
    first = se.native_active()
    second = se.native_active()
    check("the memo answers the same thing twice", first is True and second is True)
    mtime = NATIVE.stat().st_mtime_ns
    os.utime(NATIVE, ns=(mtime + 10_000_000, mtime + 10_000_000))
    check("touching the overlay invalidates the memo", se.native_active() is True)

    # ---- 3. a user-chosen engine_exe is never overridden -------------------
    se._NATIVE_MEMO = None
    check("the default engine name resolves to the overlay",
          se.engine_exe_override("engine\\wfsim-core.exe") == str(NATIVE))
    check("a pinned absolute engine is left alone",
          se.engine_exe_override(r"C:\somewhere\mine.exe") is None)
    check("engine_path() is the overlay while it matches",
          se.engine_path() == NATIVE)
    se._NATIVE_MEMO = None
    write_state(**{"engine": "moved-on", "native": dict(nat_ok)})
    check("when it stops matching, the configured name is used",
          se.engine_exe_override("engine\\wfsim-core.exe") is None
          and se.engine_path() == GENERIC)

# ---- 4. installing the overlay never touches the generic core -------------
with Swap(se, **S):
    generic_before = GENERIC.read_bytes()
    write_state(**{"engine": "85363d8320d9", "native": None})
    with Swap(se, DEPS_MARKER=MARKER):
        make_pack()
        out = se.install_native_core(built, pgo=True)
    check("install_native_core writes wfsim-core.native.exe", out == NATIVE and NATIVE.exists())
    check("the generic core is byte-identical afterwards",
          GENERIC.read_bytes() == generic_before)
    st = json.loads(STATE.read_text(encoding="utf-8"))
    check("the native build is recorded in state.json",
          (st.get("native") or {}).get("engine") == "85363d8320d9"
          and (st.get("native") or {}).get("pgo") is True
          and (st.get("native") or {}).get("sha256") == se.file_sha256(NATIVE), st.get("native"))
    check("and it reads as active right away", se.native_active() is True)

# ---- 5. refusals are honest, and staging the patches is done for the user --
with Swap(se, **S, ROOT=TMP):
    se._NATIVE_MEMO = None
    MARKER.unlink()
    raised = None
    try:
        se.build_native_core()
    except RuntimeError as e:
        raised = str(e)
    check("without the pack the build refuses, and says what to unpack",
          raised is not None and "dependency pack" in raised, raised)
    make_pack()
    old_vs = se.vs_bin
    se.vs_bin = lambda prefer="": None
    raised = None
    try:
        se.build_native_core()
    except RuntimeError as e:
        raised = str(e)
    finally:
        se.vs_bin = old_vs
    check("without a toolset the build refuses, and names Visual Studio",
          raised is not None and "Visual Studio" in raised, raised)
    PATCHES.mkdir(parents=True, exist_ok=True)
    for p in PATCHES.glob("*.patch"):
        p.unlink()
    se.ensure_patches()
    check("ensure_patches() copies the pack's patches into engine-src/patches",
          (PATCHES / "a.patch").exists())

# ---- 6. the settings screen: the third action, and which core runs --------
class FakeFleet:
    def __init__(self):
        self.cfg = {"engine_exe": "engine\\wfsim-core.exe"}
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


def status_for(kind: str, native_file: bool = False) -> dict:
    return {"kind": kind, "native_ok": kind == "native", "native_file": native_file,
            "native": {"pgo": True, "engine": "85363d8320d9"} if kind == "native" else {},
            "engine": "85363d8320d9", "cpu": "test", "deps": True, "generic": True}


async def screen_for(kind: str, deps: bool, native_file: bool = False):
    old_deps, old_status = se.deps_pack, se.engine_status
    se.deps_pack = (lambda: {"engine": "85363d8320d9"}) if deps else (lambda: None)
    se.engine_status = (lambda: status_for(kind, native_file))
    try:
        scr = appmod.SettingsScreen(FakeFleet())
        app = Host(scr)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ids = {b.id for b in scr.query("Button")}
            kind_text = scr.query_one("#engine_kind_value", Static).render().plain
            note = scr.query_one("#settingsnote", Static).render().plain
            native_text = (scr.query_one("#native_value", Static).render().plain
                           if "build_native" in ids else "")
        return ids, kind_text, native_text, note
    finally:
        se.deps_pack, se.engine_status = old_deps, old_status


ids_yes, kind_native, native_yes, note_yes = asyncio.run(screen_for("native", True))
ids_no, kind_generic, _, note_no = asyncio.run(screen_for("generic", False))
ids_stale, _, native_stale, _ = asyncio.run(
    screen_for("generic", True, native_file=True))
check("with the pack, the settings page grows 编译本机最快引擎",
      "build_native" in ids_yes, sorted(ids_yes))
check("without the pack there is no such button", "build_native" not in ids_no, sorted(ids_no))
check("the running engine is named: native", "native" in kind_native, kind_native)
check("the running engine is named: generic", "v3" in kind_generic, kind_generic)
check("the native row says the profile was used", "PGO" in native_yes, native_yes)
check("a native core that no longer matches is called out",
      "过期" in native_stale, native_stale)
check("the notes explain what the pack unlocks", "本机最快引擎" in note_yes)
check("and say nothing about it without the pack", "本机最快引擎" not in note_no)

# ---- 7. the build cannot silently reach the network ----------------------
src = (ROOT / "src" / "sync_engine.py").read_text(encoding="utf-8")
check("the native cargo build is offline by construction",
      '"CARGO_NET_OFFLINE": "true"' in src)
check("the scratch directory is chosen to be pure ASCII",
      'if all(ord(c) < 128 for c in str(p))' in src)
check("the toolset comes from vswhere, never a hardcoded VS path",
      "vswhere" in src and "VS_BUILD_TOOLS_2022" in src)
check("--build-native and --native-status exist", 'add_argument("--build-native"' in src
      and 'add_argument("--native-status"' in src)
bat = (ROOT / "ci" / "deps" / "build-engine.bat").read_text(encoding="utf-8")
check("the shipped .bat runs the local line, not the GNU one",
      "--build-native" in bat and "--build-only" not in bat)
fleet_src = (ROOT / "src" / "fleet.py").read_text(encoding="utf-8")
check("the fleet asks the overlay before starting a core",
      "sync_engine.engine_exe_override(exe)" in fleet_src)
check("the overlay is a separate file name",
      'NATIVE_CORE = ENGINE / "wfsim-core.native.exe"' in src)

print("\n%d passed, %d failed" % (ok, bad))
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if bad else 0)
