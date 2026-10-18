# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""The TUI: what the fleet is doing, and the knobs worth having.

    run-portable.bat            (double-click in the portable folder)

Keys:  q 退出   p 暂停/继续   + / -  常驻 verifier 数     s 立刻刷新
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time

# Embedded Python uses a ._pth file, which does NOT automatically put the
# script's own folder on sys.path.  Add it explicitly so the portable copy
# works after being moved to any drive or folder.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import paths

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Button, DataTable, Footer, Header, RichLog, Static
except ModuleNotFoundError as _e:                      # Textual missing
    if _e.name and _e.name.split(".")[0] == "textual":
        import sys
        print("这个 Python 没装 Textual：")
        print("   ", sys.executable)
        print()
        print("两种办法，任选一种：")
        print("   1) 双击 run.bat —— 它会自己挑一个装了 Textual 的解释器")
        print("   2) 装一次：  py -3 -m pip install textual")
        try:
            input("\n按回车关闭…")
        except Exception:
            pass
        raise SystemExit(1)
    raise

import config
import cores
import sync_engine          # has a __main__ guard, so importing runs nothing
import ui_scale
from fleet import Fleet, Verifier, new_verifier_id
from task_log import normalise_level

#: The two log levels, in the words the settings page shows.  The stored value
#: is the English key (stable in config.json); the page shows the label.
LOG_LEVEL_LABELS = {"off": "关", "full": "完整"}

# TWO COLOURS ONLY: black background with white text, and orange background
# with white text for every title bar, chip, button and log tag.
CSS = """
Screen { background: #000000; color: #eaeaea; }
#body { height: 1fr; }
#left  { width: 74%; }
#right { width: 26%; border-left: solid #2a2a2a; }
DataTable { background: #000000; }
#vers { height: 12; }
#slots { padding: 0; height: 15; }
#slots-body { padding: 0; height: auto; }
RichLog { height: 1fr; min-height: 6; border-top: solid #d97706; background: #000000; }
#pool { padding: 0 1; height: 17; background: #000000; }
#pat { padding: 0 1; height: 1; }
#rank-title { padding: 0 1; height: 1; }
#rank { height: 1fr; min-height: 4; border-top: solid #d97706; background: #000000; }
#head { padding: 0 1; height: 1; background: #111111; }
Footer { background: #111111; color: #ffffff; }
FooterKey { background: #111111; }
FooterKey .footer-key--key { padding: 0; }
SettingsScreen { background: #000000; color: #eaeaea; }
#settingshead { padding: 1 2; height: 6; }
/* The rows, the plates and the note live in SettingsScreen.CSS; the rules that
   used to sit here were overridden by it and only invited drift. */
"""


class PriorityTable(DataTable):
    """DataTable where one mouse click selects the cell immediately.

    Textual's default first click only moves the cursor; this table drives the
    explicit status/prefetch priority action, so it intentionally posts the
    selection on the first click.
    """

    async def _on_click(self, event) -> None:
        meta = getattr(getattr(event, "style", None), "meta", None) or {}
        if "row" not in meta or "column" not in meta:
            return await super()._on_click(event)
        if self.cursor_type != "row" and meta.get("out_of_bounds", False):
            return
        row, col = int(meta["row"]), int(meta["column"])
        if row < 0 or col < 0:
            return await super()._on_click(event)
        self.cursor_coordinate = (row, col)
        self._post_selected_message()
        event.stop()


def core_source() -> str:
    """Which update line this package is on: `prebuilt` or `local`.

    Local reads only — `engine-src/state.json`'s `core_source`, the `prebuilt`
    flag CI writes, and then what is actually on disk (no `toolchain/bin/
    cargo.exe`, no `patches/`).  A package that came off the release page can
    never build: it has neither the toolchain nor the source tree, so it must
    not be offered a build button, and it must not pause the fleet every five
    minutes to run a cargo that is not there.
    """
    try:
        return str(sync_engine.core_source() or sync_engine.CORE_SOURCE_LOCAL)
    except Exception:
        return sync_engine.CORE_SOURCE_LOCAL


#: The value column is 16 cells wide; these four fit whole.
ENGINE_KIND_LABELS = {"native": "本机 native", "generic": "通用 v3",
                      "gnu": "本地编译"}


def engine_kind_text() -> tuple[str, str]:
    """`(运行引擎 列, 编译行的状态列)` read straight from the engine state.

    Which core runs is decided by `sync_engine.native_active()`, so the page asks
    the same function instead of keeping a second copy of the rule.
    """
    try:
        st = sync_engine.engine_status()
    except Exception:
        return "?", ""
    kind = str(st.get("kind") or "?")
    value = ENGINE_KIND_LABELS.get(kind, kind)
    if kind == "native":
        nat = st.get("native") or {}
        note = "已启用 PGO" if nat.get("pgo") else "无 PGO"
        return value, note
    if st.get("native_file"):
        # A native core exists but is being ignored on purpose.  "another
        # revision" and "another CPU" are different problems with different
        # cures, so the row says which one it is instead of a silent downgrade.
        if st.get("native_reason") == "cpu":
            return value, "native 换机失效"
        return value, "native 已过期"
    return value, ("未编译" if st.get("deps") else "")


class SettingsScreen(Screen):
    """A compact card of one-row settings."""

    BINDINGS = [("escape", "app.pop_screen", "返回")]

    CSS = """
    #settingshead { padding: 1 2; height: 5; }
    #settingscard {
        width: 92; margin: 0 2 1 2; padding: 0 1; height: auto;
        border: none; background: #000000;
    }
    /* ONE LINE A CLICKABLE THING.  Every control that can be pressed wears a
       quiet 12% white plate, the choice in force wears a 25% amber plate with
       amber text, and the two actions that take minutes stand three rows tall
       (Textual's own button height) so they never read as another −/+.
       Hover brightens the plate instead of inverting to white-on-black. */
    .setting-row { height: 1; padding: 0 1; }
    .setting-row.action-row { height: 3; }
    .setting-name { width: 20; content-align: left middle; }
    .setting-value { width: 16; content-align: right middle; }
    .setting-row Button {
        height: 1; min-width: 5; margin: 0 0 0 1; padding: 0 1;
        border: none; background: #ffffff1f; color: #eaeaea;
    }
    .setting-row Button:hover { background: #ffffff33; color: #ffffff; }
    .setting-row Button:focus { background: #ffffff33; color: #ffffff;
        text-style: bold; }
    .setting-row Button.on { background: #d9770640; color: #fbbf24;
        text-style: bold; }
    .setting-row Button.on:hover, .setting-row Button.on:focus {
        background: #d9770666; color: #fde68a; }
    .setting-row Button:disabled { background: #ffffff0f; color: #4a4a4a; }
    /* A full "跟随系统" is eight cells wide; fixed width keeps every label
       whole instead of letting the row squeeze it to one character. */
    .setting-row Button.scale-btn { width: 11; min-width: 11; }
    /* The 手动 version switch: amber (the .on class) while it is on, and wide
       enough for "手动 开" — at 8 cells the state character was cut off, and a
       switch that does not say which way it is set is not a switch. */
    #wire_manual_toggle { width: 11; min-width: 11; }
    #settingsnote { padding: 1 2; height: auto; }
    #rebuild_engine, #rebuild_fast {
        height: 3; min-width: 11; text-style: bold;
        background: #d9770640; color: #fbbf24;
    }
    #rebuild_engine:hover, #rebuild_fast:hover {
        background: #d9770666; color: #fde68a; }
    #rebuild_engine:disabled, #rebuild_fast:disabled {
        background: #ffffff0f; color: #4a4a4a; }
    """

    def __init__(self, fleet, on_change=None, engine_state=None,
                 on_engine_shift=None, on_engine_rebuild=None, on_scale=None,
                 on_engine_rebuild_fast=None, on_slots=None,
                 on_engine_native=None, on_wire_manual=None):
        super().__init__()
        self.fleet = fleet
        self.on_change = on_change
        self.engine_state = engine_state
        self.on_engine_shift = on_engine_shift
        self.on_engine_rebuild = on_engine_rebuild
        self.on_engine_rebuild_fast = on_engine_rebuild_fast
        self.on_scale = on_scale
        self.on_slots = on_slots
        self.on_engine_native = on_engine_native
        self.on_wire_manual = on_wire_manual
        self._refreshing = False
        #: A prebuilt ("通用引擎") package has no rust toolchain and no source
        #: tree, so the two build actions cannot ever run here.  They are
        #: HIDDEN rather than shown-disabled: an option that cannot be honoured
        #: is not an option to put in front of the user.
        self.prebuilt = core_source() == sync_engine.CORE_SOURCE_PREBUILT
        #: The optional self-build dependency pack (`build-deps.json` at the root
        #: of the folder).  It is what makes a prebuilt package able to compile a
        #: native engine, so the third build action exists exactly when it does.
        try:
            self.deps = sync_engine.deps_pack() is not None
        except Exception:
            self.deps = False

    def compose(self):
        yield Header()
        yield Static("", id="settingshead")
        with Vertical(id="settingscard"):
            with Horizontal(classes="setting-row"):
                yield Static("上报服务器版本", classes="setting-name")
                yield Static("", id="engine_value", classes="setting-value")
                yield Button("−", id="engine_minus")
                yield Button("+", id="engine_plus")
                yield Button("手动 关", id="wire_manual_toggle")
            with Horizontal(classes="setting-row"):
                yield Static("常驻验证器", classes="setting-name")
                yield Static("", id="active_value", classes="setting-value")
                yield Button("−", id="active_minus")
                yield Button("+", id="active_plus")
            with Horizontal(classes="setting-row"):
                yield Static("计算进程", classes="setting-name")
                yield Static("", id="slots_value", classes="setting-value")
                yield Button("−", id="slots_minus")
                yield Button("+", id="slots_plus")
            with Horizontal(classes="setting-row"):
                yield Static("小批量上传模式", classes="setting-name")
                yield Static("", id="small_batch_value", classes="setting-value")
                yield Button("关", id="small_batch_toggle")
            with Horizontal(classes="setting-row"):
                yield Static("日志详细级别", classes="setting-name")
                yield Static("", id="log_level_value", classes="setting-value")
                yield Button("切换", id="log_level_toggle")
            with Horizontal(classes="setting-row"):
                yield Static("任务落盘(额外副本)", classes="setting-name")
                yield Static("", id="task_persist_value", classes="setting-value")
                yield Button("关", id="task_persist_toggle")
            with Horizontal(classes="setting-row"):
                yield Static("Board协议", classes="setting-name")
                yield Static("协议 8", id="board_proto_value",
                             classes="setting-value")
            # WHICH ENGINE IS RUNNING.  A package can carry a downloaded generic
            # core, a core this machine compiled for itself (`native`) or one
            # built from source (`gnu`); the difference is 2-3x and invisible
            # from the outside, so it is stated here and again at startup.
            with Horizontal(classes="setting-row"):
                yield Static("运行引擎", classes="setting-name")
                yield Static("", id="engine_kind_value", classes="setting-value")
            # The two build actions exist only on the self-compiled line; on a
            # prebuilt package they are not yielded at all (see `self.prebuilt`).
            if not self.prebuilt:
                with Horizontal(classes="setting-row action-row"):
                    yield Static("清理缓存重建引擎", classes="setting-name")
                    yield Static("本机 CPU 优化", classes="setting-value")
                    yield Button("重建", id="rebuild_engine")
                with Horizontal(classes="setting-row action-row"):
                    yield Static("快速重新编译引擎", classes="setting-name")
                    yield Static("增量，不清理缓存", classes="setting-value")
                    yield Button("编译", id="rebuild_fast")
            # The local line, both package kinds: needs only the dependency pack
            # (everything except Visual Studio), and never replaces the generic
            # core -- see `sync_engine.install_native_core`.
            if self.deps:
                with Horizontal(classes="setting-row action-row"):
                    yield Static("编译本机最快引擎", classes="setting-name")
                    yield Static("", id="native_value", classes="setting-value")
                    yield Button("编译", id="build_native")
            with Horizontal(classes="setting-row"):
                yield Static("界面缩放", classes="setting-name")
                yield Static("", id="scale_value", classes="setting-value")
                for pct in ui_scale.SCALE_CHOICES[:4]:
                    yield Button(ui_scale.label(pct), id=f"scale{pct}",
                                 classes="scale-btn")
            with Horizontal(classes="setting-row"):
                yield Static("", classes="setting-name")
                yield Static("", classes="setting-value")
                for pct in ui_scale.SCALE_CHOICES[4:]:
                    yield Button(ui_scale.label(pct), id=f"scale{pct}",
                                 classes="scale-btn")
        yield Static("", id="settingsnote")
        yield Footer()

    def on_mount(self):
        self._refresh()

    def _refresh(self):
        self._refreshing = True
        try:
            active = int(self.fleet.cfg.get("active_verifiers") or 1)
            maximum = int(self.fleet.cfg.get("max_verifiers") or active)
            enabled = bool(self.fleet.cfg.get("small_batch_upload"))
            engine_text, engine_note, can_prev, can_next = (
                self.engine_state() if self.engine_state else
                (str(self.fleet.wire_engine or self.fleet.engine_id or "?"), "", False, False))
            self.query_one("#engine_value", Static).update(engine_text)
            # The 手动 switch gates the whole row: while it is off, the stored
            # engine id is not read and the ± buttons do nothing.
            manual = bool(self.fleet.cfg.get("wire_manual"))
            if not manual:
                can_prev = can_next = False
            self.query_one("#engine_minus", Button).disabled = not can_prev
            self.query_one("#engine_plus", Button).disabled = not can_next
            manual_button = self.query_one("#wire_manual_toggle", Button)
            manual_button.label = "手动 开" if manual else "手动 关"
            manual_button.set_class(manual, "on")
            self.query_one("#active_value", Static).update(f"{active} / {maximum}")
            self.query_one("#small_batch_value", Static).update("开" if enabled else "关")
            toggle = self.query_one("#small_batch_toggle", Button)
            toggle.label = "开" if enabled else "关"
            toggle.set_class(enabled, "on")
            # log level: 关 writes nothing, 完整 keeps every body and every
            # scheduling record.
            level = normalise_level(self.fleet.cfg.get("task_logging"))
            self.query_one("#log_level_value", Static).update(
                LOG_LEVEL_LABELS.get(level, level))
            lb = self.query_one("#log_level_toggle", Button)
            lb.label = "切换"
            lb.set_class(level != "off", "on")
            # task persist (extra copy)
            persisted = bool(self.fleet.cfg.get("task_persist"))
            self.query_one("#task_persist_value", Static).update("开" if persisted else "关")
            pb = self.query_one("#task_persist_toggle", Button)
            pb.label = "开" if persisted else "关"
            pb.set_class(persisted, "on")
            # board wire: protocol 8, one door, no switch — and nothing to
            # switch TO.  A server that answers `fallback: 7` stops this client
            # (fleet._proto8_off); it never speaks the classic wire.  The value
            # column is 16 cells wide, so the parenthetical lives in the notes
            # below instead of being trimmed to "协议 8（一".
            paused = getattr(self.fleet, "_proto8_off", False)
            self.query_one("#board_proto_value", Static).update(
                "协议 8 · 已暂停" if paused else "协议 8")
            # which core the fleet would start right now, and why
            busy = bool(getattr(self.fleet, "maintenance", False))
            kind, kind_note = engine_kind_text()
            self.query_one("#engine_kind_value", Static).update(kind)
            if self.deps:
                self.query_one("#native_value", Static).update(kind_note)
                self.query_one("#build_native", Button).disabled = busy
            scale = int(self.fleet.cfg.get("ui_scale_percent") or 0)
            self.query_one("#scale_value", Static).update(ui_scale.label(scale))
            for pct in ui_scale.SCALE_CHOICES:
                self.query_one(f"#scale{pct}", Button).set_class(
                    int(pct) == scale, "on")
            if not self.prebuilt:
                self.query_one("#rebuild_engine", Button).disabled = busy
                self.query_one("#rebuild_fast", Button).disabled = busy
            slots = self.fleet.slot_count()
            self.query_one("#slots_value", Static).update(
                f"{slots} / {config.SLOTS_MAX}")
            self.query_one("#slots_minus", Button).disabled = (
                busy or not self.on_slots or slots <= config.SLOTS_MIN)
            self.query_one("#slots_plus", Button).disabled = (
                busy or not self.on_slots or slots >= config.SLOTS_MAX)
            self.query_one("#settingshead", Static).update(
                "[bold white on #d97706] 设置 [/]\n"
                "[dim]数值设置用每行的 −/+ 调整；开关直接点击。[/dim]\n"
                "[dim]Esc 返回。[/dim]")
            # The two build actions are not on this screen when the package is
            # prebuilt, so neither is the paragraph that explains them; the
            # download line says what it does instead.
            if self.prebuilt:
                build_note = (
                    "[dim]本包是预编译包（随包发的是通用引擎，不含 rust 工具链"
                    + ("）：\n" if not self.deps else "与源码树）：\n")
                    + ("没有本地编译选项。"
                       if not self.deps else
                       "没有从源码编译的选项（源码树不随包发布）；\n"
                       "有了右边的依赖包，可以直接编译本机最快的引擎。\n")
                    + "服务器下发新引擎时会自动下载、校验 sha256 后安装，\n"
                      "下载期间照常计算，只在安装前短暂排空。[/dim]\n")
            else:
                build_note = (
                    "[dim]清理重建会暂停接任务，按本机 CPU 从零编译引擎，耗时数分钟。\n"
                    "快速重编只走增量（保留 build-cache），把新的补丁编进引擎，通常快得多。\n")
            if self.deps:
                build_note += (
                    "[dim]已放入自编译依赖包：可以编译本机最快引擎（native + PGO，每台\n"
                    "机器不一样，一般比通用版快 10%~15%）。它按服务器版本自动生效，版本\n"
                    "对不上时自动回落通用引擎，通用引擎不会被覆盖。编译期间照常计算。[/dim]\n")
            self.query_one("#settingsnote", Static).update(
                (f"[dim]{engine_note}[/dim]\n" if engine_note else "") +
                "[dim]小批量上传关闭：整包完成后上传。\n"
                "小批量上传开启：每 2 条上传一次，最后不足 2 条在包结束时上传。[/dim]\n"
                "[dim]Board 协议固定为 8，一扇门、无切换；服务端哪天回 fallback 7，\n"
                "本客户端会暂停领取任务，不会退回旧协议。[/dim]\n"
                + build_note +
                "[dim]计算进程的 −/+ 会先暂停接任务、等正在算的行上传完，再按新数量重启引擎\n"
                "并重新给每个进程分配线程（默认 3，最少 1，最多 10）。\n"
                "界面缩放改的是当前 Windows Terminal 配置档的字号（50%~200%），\n"
                "选“跟随系统”恢复原来的字号；非 Windows Terminal 可能不生效。[/dim]")
        finally:
            self._refreshing = False

    def _save(self, text: str) -> None:
        try:
            config.save(self.fleet.cfg)
        except Exception:
            pass
        self.fleet.on_event("settings", text)
        self._refresh()
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _toggle_small_batch(self) -> None:
        enabled = not bool(self.fleet.cfg.get("small_batch_upload"))
        self.fleet.cfg["small_batch_upload"] = enabled
        self._save(f"小批量上传模式 -> {'开' if enabled else '关'}")



    def _start_rebuild(self, button: Button, action=None,
                       label: str = "重建") -> None:
        action = action or self.on_engine_rebuild
        if not action:
            return
        button.disabled = True
        button.label = "编译中"
        asyncio.create_task(self._run_rebuild(button, action, label))

    async def _run_rebuild(self, button: Button, action=None,
                           label: str = "重建") -> None:
        action = action or self.on_engine_rebuild
        try:
            await action()
        finally:
            try:
                button.label = label
                button.disabled = False
                self._refresh()
            except Exception:
                pass

    def _start_engine_shift(self, delta: int) -> None:
        if not self.on_engine_shift:
            return
        # While 手动 is off the stored id is not read, so moving it would only
        # pretend to do something; the buttons are disabled too, this is the
        # second gate for a click that slips through.
        if not bool(self.fleet.cfg.get("wire_manual")):
            return
        asyncio.create_task(self._run_engine_shift(delta))

    async def _run_engine_shift(self, delta: int) -> None:
        try:
            await self.on_engine_shift(delta)
        finally:
            self._refresh()

    def _on_wire_manual(self, event) -> None:
        if not self.on_wire_manual:
            return
        asyncio.create_task(self._run_wire_manual())

    async def _run_wire_manual(self) -> None:
        try:
            await self.on_wire_manual(not bool(self.fleet.cfg.get("wire_manual")))
        finally:
            self._refresh()

    def _change_active(self, delta: int) -> None:
        current = int(self.fleet.cfg.get("active_verifiers") or 1)
        if delta < 0:
            value = max(1, current - 1)
        else:
            value = min(int(self.fleet.cfg.get("max_verifiers") or 1), current + 1)
        self.fleet.cfg["active_verifiers"] = value
        self._save(f"常驻验证器 -> {value}")

    _BUTTON_ACTIONS = {
        "small_batch_toggle": "_on_small_batch",
        "log_level_toggle": "_on_log_level",
        "task_persist_toggle": "_on_task_persist",
        "rebuild_engine": "_on_rebuild_engine",
        "rebuild_fast": "_on_rebuild_fast",
        "build_native": "_on_build_native",
        "engine_minus": "_on_engine_minus",
        "engine_plus": "_on_engine_plus",
        "wire_manual_toggle": "_on_wire_manual",
        "active_minus": "_on_active_minus",
        "active_plus": "_on_active_plus",
        "slots_minus": "_on_slots_minus",
        "slots_plus": "_on_slots_plus",
        **{f"scale{pct}": "_on_scale" for pct in ui_scale.SCALE_CHOICES},
    }

    def _on_small_batch(self, event) -> None:
        self._toggle_small_batch()

    def _on_log_level(self, event) -> None:
        #: 关 -> 完整 -> 关.  `enabled` is part of the toggle: without it,
        #: switching to 关 would leave the logger writing, and switching back
        #: on would find it still switched off.
        order = ["off", "full"]
        current = normalise_level(self.fleet.cfg.get("task_logging"))
        value = order[(order.index(current) + 1) % len(order)]
        self.fleet.cfg["task_logging"] = value
        log = self.fleet.log
        log.mode = value
        log.enabled = value != "off"
        log.full = value == "full"
        self._save(f"日志级别 -> {LOG_LEVEL_LABELS.get(value, value)}")
        self._refresh()

    def _on_task_persist(self, event) -> None:
        value = not bool(self.fleet.cfg.get("task_persist"))
        self.fleet.cfg["task_persist"] = value
        self.fleet._task_persist = value
        self._save(f"任务落盘 -> {'开' if value else '关'}")
        self._refresh()

    def _on_scale(self, event) -> None:
        bid = event.button.id or ""
        try:
            percent = int(bid[5:])
        except Exception:
            return
        if percent not in ui_scale.SCALE_CHOICES:
            return
        self.fleet.cfg["ui_scale_percent"] = percent
        if self.on_scale:
            try:
                self.on_scale(percent)
            except Exception:
                pass
        self._refresh()


    def _on_rebuild_engine(self, event) -> None:
        self._start_rebuild(event.button)

    def _on_rebuild_fast(self, event) -> None:
        # THE INCREMENTAL ONE: the same flow as the clean rebuild without
        # `--clean`, so a patch change is compiled into the engine without
        # rebuilding the world.  It is what applies a new patch set to a machine
        # that is otherwise up to date.
        self._start_rebuild(event.button, self.on_engine_rebuild_fast, label="编译")

    def _on_build_native(self, event) -> None:
        # THE LOCAL LINE: builds with the toolset found on this machine, from the
        # dependency pack.  It is the same action on both package kinds -- the
        # pack is what it needs, not the source tree.
        self._start_rebuild(event.button, self.on_engine_native, label="编译")

    def _on_engine_minus(self, event) -> None:
        self._start_engine_shift(-1)

    def _on_engine_plus(self, event) -> None:
        self._start_engine_shift(1)

    def _on_active_minus(self, event) -> None:
        self._change_active(-1)

    def _on_active_plus(self, event) -> None:
        self._change_active(1)

    def _on_slots_minus(self, event) -> None:
        self._start_slot_change(-1)

    def _on_slots_plus(self, event) -> None:
        self._start_slot_change(1)

    def _start_slot_change(self, delta: int) -> None:
        if not self.on_slots:
            return
        asyncio.create_task(self._run_slot_change(delta))

    async def _run_slot_change(self, delta: int) -> None:
        try:
            await self.on_slots(delta)
        finally:
            self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = self._BUTTON_ACTIONS.get(event.button.id or "")
        if handler is None:
            return
        getattr(self, handler)(event)
        event.stop()



class CoresScreen(Screen):
    """A SECOND PAGE: which logical CPUs the engine processes may run on.

    It talks about cores the way the system does — `CPU 0` … `CPU 23`, the names
    Task Manager shows — so a limit set here can be checked there. The affinity
    goes on the PROCESS, because one engine is one process with a thread pool.
    """

    BINDINGS = [("escape", "app.pop_screen", "返回"), ("space", "toggle", "勾选/取消")]

    CSS = """
    #corehead { padding: 1 2; }
    #cpulimit { height: 3; align: center middle; }
    #cpulimit Button { margin: 0 1; min-width: 6; background: #d97706;
                       color: #ffffff; border: none; }
    #coretype { height: 3; align: center middle; }
    #coretype Button { margin: 0 1; min-width: 13; background: #3a3a3a;
                       color: #e6e6e6; border: none; }
    #coretype Button.on { background: #d97706; color: #ffffff; }
    DataTable { height: 1fr; background: #000000; }
    """

    def __init__(self, fleet, on_change, on_limit=None, on_core_type=None):
        super().__init__()
        self.fleet = fleet
        self.on_change = on_change
        self.on_limit = on_limit
        self.on_core_type = on_core_type
        self.core_type = str(fleet.cfg.get("core_type") or "auto").lower()
        self.hybrid = cores.hybrid()
        self.topo = cores.topology()
        self.ccd = cores.ccd_map()
        chosen = [int(c) for c in (fleet.cfg.get("engine_cores") or [])]
        self.selected = set(chosen) if chosen else {c["cpu"] for c in self.topo}
        try:
            self.cpu_limit_percent = max(10, min(100, int(
                fleet.cfg.get("cpu_limit_percent") or 100)))
        except Exception:
            self.cpu_limit_percent = 100

    def compose(self):
        yield Header()
        yield Static("", id="corehead")
        with Horizontal(id="coretype"):
            for ct in cores.CORE_TYPES:
                b = Button(cores.core_type_label(ct), id=f"ct{ct}")
                if ct == self.core_type:
                    b.add_class("on")
                yield b
        with Horizontal(id="cpulimit"):
            for pct in range(10, 101, 10):
                yield Button(f"{pct}%", id=f"lim{pct}")
        yield DataTable(id="cores")
        yield Footer()

    def on_mount(self):
        t = self.query_one("#cores", DataTable)
        t.add_columns("CPU（系统名称）", "Windows 等级", "CCD/L3", "引擎池",
                      "进程槽", "分配给", "同核线程")
        t.cursor_type = "row"
        owners = self._slot_owners()
        # WHICH COMPUTE PROCESS A THREAD SERVES — with an explicit warning when
        # a core would be SPLIT across slots or a CCD boundary: the planner is
        # not supposed to do either, and this column makes both visible.
        client_cpu = self.fleet.cfg.get("log_cpu")
        try:
            client_cpu = int(client_cpu)
        except (TypeError, ValueError):
            client_cpu = -1

        def owner_of(cpu: int) -> str:
            return next((f"槽{i + 1}" for i, s in enumerate(owners)
                         if cpu in s), "")

        for c in self.topo:
            kind = ""
            if self.hybrid:
                pset = {x["index"] for x in self.hybrid["p"]}
                kind = " [b]P[/b]" if c["cpu"] in pset else " [dim]E[/dim]"
            owner = owner_of(c["cpu"])
            sibs = list(c.get("siblings") or [])
            sib_owners = {o for o in (owner_of(x) for x in sibs) if o}
            ccds = {str(self.ccd.get(x, "?")) for x in sibs}
            ccds.add(str(self.ccd.get(c["cpu"], "?")))
            note = (" [red]⚠劈核[/red]" if len(sib_owners) > 1
                    else " [red]⚠跨CCD[/red]" if len(ccds) > 1 else "")
            if owner:
                assign = f"[bold]{owner}[/]{note}"
            elif c["cpu"] == client_cpu:
                assign = "客户端（界面）"
            else:
                assign = "[dim]未分配（系统）[/]"
            t.add_row(cores.name(c["cpu"]), f"class {c['efficiency']}{kind}",
                      str(self.ccd.get(c["cpu"], "?")),
                      "是" if c["cpu"] in self.selected else "[dim]否[/dim]",
                      owner or "[dim]—[/dim]", assign,
                      ",".join(str(x) for x in c["siblings"]))
        self._head()

    def _balanced_plan(self):
        """The same fallback ladder the fleet uses, for what the page shows."""
        slots = self.fleet.slot_count()
        try:
            groups, primary, note = cores.schedule_balanced(slots, self.selected)
            if groups and all(groups):
                return groups, primary, note
            groups, note = cores.schedule_groups(slots, self.selected)
            if groups and all(groups):
                return groups, [], note
            groups, note = cores.schedule_even(slots, self.selected)
            return groups, [], note
        except Exception:
            return [], [], ""

    def _slot_owners(self) -> list[set]:
        if self.core_type != "max":
            return []
        groups, _, _ = self._balanced_plan()
        sets = cores.cpu_sets()
        return [set(cores.logical_of_set_ids(g, sets)) for g in groups]

    def _plan_line(self) -> str:
        """The '<= 均衡分组 3P+5E / ...' line, or '' outside max mode."""
        if self.core_type != "max":
            return ""
        groups, _, note = self._balanced_plan()
        if not groups:
            return ""
        sets = cores.cpu_sets()
        shapes = " / ".join(f"{p}P+{e}E" for p, e in
                            (cores.plan_pk(g, sets) for g in groups))
        return f"[b]均衡分组[/b] {note}   每槽 {shapes}\n"

    def _head(self):
        n_all = len(self.topo)
        n = len(self.selected)
        actual = self.fleet.cpu_limit_actual
        cap = (f"{self.cpu_limit_percent}%"
               if actual is None else
               f"{self.cpu_limit_percent}%（Windows 实际生效 {actual}%）")
        if self.hybrid:
            hp = len(self.hybrid["p"])
            he = len(self.hybrid["e"])
            hy = (f"[b]Intel 大小核[/b]  P 核 {hp} 个线程 / E 核 {he} 个线程"
                  f"（按 EfficiencyClass 判定，等级最高的是 P 核）")
        else:
            hy = f"[b]大小核[/b] {cores.vendor() or '?'} —— 不适用"
        l3 = sorted({v for v in self.ccd.values()})
        if len(l3) > 1:
            ccd = (f"[b]CCD[/b] 检测到 {len(l3)} 个 L3/CCD 组："
                   + ", ".join(str(x) for x in l3))
        else:
            ccd = f"[b]CCD[/b] 单 L3 组（CCD 检测不生效）"
        smt = cores.smt_info()
        if smt["enabled"]:
            smt_line = (f"[b]超线程[/b] 开（每核 {smt['threads_per_core']} 线程，"
                        f"{smt['physical_cores']} 核 / {smt['logical_threads']} 线程）")
        else:
            smt_line = (f"[b]超线程[/b] 关（{smt['physical_cores']} 核 / "
                        f"{smt['logical_threads']} 线程）")
        plan = self._plan_line()
        self.query_one("#corehead", Static).update(
            f"[b]引擎 CPU 池（手动）[/b]   {n} / {n_all} 个逻辑处理器可用\n"
            f"[b]CPU 上限[/b] {cap}   "
            f"[dim]10%~100%，每档 10%，由 Windows 硬限制[/dim]\n"
            f"{hy}\n"
            f"{ccd}\n"
            f"{smt_line}\n"
            f"{plan}"
            f"[b]核心类型[/b] {cores.core_type_label(self.core_type)}   "
            f"[dim]只安排计算线程跑在哪类核上，不改变计算结果[/dim]\n"
            f"[dim]空格切换高亮核心（手动池）；Esc 返回。手动池加在进程上，"
            f"没勾的核心仍可能被系统分给界面和其它程序。[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("ct"):
            ct = bid[2:]
            if ct not in cores.CORE_TYPES:
                return
            self.core_type = ct
            for c in cores.CORE_TYPES:
                btn = self.query_one(f"#ct{c}", Button)
                btn.set_class(c == ct, "on")
            self._head()
            if self.on_core_type:
                self.on_core_type(ct)
            event.stop()
            return
        if not bid.startswith("lim"):
            return
        try:
            pct = int(bid[3:])
        except Exception:
            return
        self.cpu_limit_percent = max(10, min(100, pct))
        self._head()
        if self.on_limit:
            self.on_limit(self.cpu_limit_percent)
        event.stop()

    def action_toggle(self):
        t = self.query_one("#cores", DataTable)
        if t.cursor_row is None or t.cursor_row >= len(self.topo):
            return
        cpu = self.topo[t.cursor_row]["cpu"]
        if cpu in self.selected:
            self.selected.discard(cpu)
        else:
            self.selected.add(cpu)
        t.update_cell_at((t.cursor_row, 2),
                         "是" if cpu in self.selected else "[dim]否[/dim]")
        self._head()
        # APPLIED AT ONCE, so the effect can be watched in Task Manager while
        # the page is open.
        self.on_change(sorted(self.selected))

#: The files whose newest mtime IS this copy's version.
_VERSION_FILES = ("app.py", "fleet.py", "api.py", "cores.py", "config.py",
                  "work_model.py", "verifier_fsm.py", "task_log.py",
                  "sync_engine.py", "ui_scale.py")


def build_stamp() -> str:
    """When this copy was last updated, to the second.

    A version number you can quote in a bug report without inventing a
    numbering scheme: it is the newest mtime of the client's own sources.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    newest = 0.0
    for name in _VERSION_FILES:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(here, name)))
        except OSError:
            pass
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest))
            if newest else "unknown")


#: The engine's `phase`, short enough for a narrow column.  Used whenever a
#: riven has no percentage yet — `sims_planned` is 0 until the funnel rounds
#: start, so early on the phase is the only progress there is.
PHASE_ZH = {"enumerating": "枚举", "running": "搜索", "done": "收尾",
            "cancelled": "取消", "error": "出错"}

#: `_event` kinds worth keeping on disk.  Everything else lives only in the
#: scrolling pane because it fires far too often to write down.
_PERSIST_UI = {"error", "update", "deadline", "engine_stale", "release",
               "settings", "cores", "cpu", "stop"}


def _hard_exit() -> None:
    """Quit now, with no cleanup, from a plain thread."""
    os._exit(0)


class WfSimClient(App):
    CSS = CSS
    BINDINGS = [
        Binding("q", "shutdown", "退出并归还任务", key_display=""),
        Binding("p", "toggle_pause", "暂停/继续", key_display=""),
        Binding("t", "open_settings", "设置", key_display=""),
        Binding("s", "tick_now", "刷新", key_display=""),
        Binding("u", "check_update", "检查更新", key_display=""),
        Binding("a", "add_verifier", "＋验证器", key_display=""),
        Binding("d", "drop_verifier", "－验证器", key_display=""),
        Binding("r", "refresh_verifiers", "刷新验证器", key_display=""),
        Binding("c", "open_cores", "CPU核心/限速", key_display=""),
    ]

    def get_key_display(self, binding: Binding) -> str:
        """Hide shortcut prefixes in the clickable Footer.

        Textual treats an empty `key_display` as "not set" and falls back to
        the key itself.  The FooterKey still keeps the real key and simulates
        it on click, so returning a literal empty string only changes display.
        """
        if binding.key_display == "":
            return ""
        return super().get_key_display(binding)

    def __init__(self, cfg: dict):
        super().__init__()
        # THE TITLE CARRIES THE VERSION so a screenshot identifies the build.
        self.title = f"WfSimClient {build_stamp()}"
        self.cfg = cfg
        self.fleet: Fleet | None = None
        self._inflight = False
        self.rounds = 0
        self.ready = False
        self._startup: asyncio.Task | None = None
        self._updating = False
        #: True while a WHOLE-PACKAGE download is staged: it replaces src/ as
        #: well as engine/, so finishing it means relaunching this client.
        self._package_pending = False
        #: True once `--install-portable` has actually overwritten src/ and
        #: engine/.  From that moment there is no old tree left to roll back to
        #: and the package's own state.json is the truth.
        self._package_installed = False
        #: GitHub's switchable-engine catalog, refreshed on the same 10-minute
        #: timer as the source update check.
        self._engine_catalog: dict | None = None
        self._catalog_sig: tuple | None = None
        self._engine_switch_lock = asyncio.Lock()
        self._stale_update_task: asyncio.Task | None = None
        self._auto_update_task: asyncio.Task | None = None
        #: ONE timer, TEN minutes, TWO signals: `engine` and `optimizer/src`
        self._update_check_at = 0.0
        self._update_check_interval = 600.0
        #: WHICH UPDATE LINE THIS COPY IS — "prebuilt" (the CI zip: a compiled
        #: core, no toolchain) or "local" (carries the rust toolchain and builds
        #: from sources).  Read once from the package's own state and cached:
        #: nothing about the package changes while the console runs.
        self._core_source: str | None = None
        #: a failed rebuild backs off instead of hammering the same broken
        #: upstream every ten minutes forever
        self._update_backoff = 300.0
        self._update_retry_at = 0.0
        self._closing = False
        self._shutdown_watchdog = None
        self._pat_state = {"state": "checking"}
        self._pat_check_at = 0.0
        self._leaderboard_at = -1.0
        #: the last fault each panel reported, so a panel that faults on every
        #: tick is logged once per distinct failure instead of twice a second
        self._paint_errors: dict[str, str] = {}

    # ------------------------------------------------------------- compose
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("正在启动…", id="head")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield PriorityTable(id="vers")
                # A SCROLLING PANE, not a fixed-height label.  Each compute
                # process draws five lines, so three processes exactly fill the
                # old `height: 15` and a fourth or sixth were clipped off the
                # bottom with no way to see them.  Wrapping the text in a
                # `VerticalScroll` keeps the same viewport height but adds a
                # scrollbar the reader drives, so any number of processes is
                # reachable and nothing repaints the offset away.
                with VerticalScroll(id="slots"):
                    yield Static("", id="slots-body")
                yield RichLog(id="log", highlight=True, markup=True, max_lines=100000)
            with Vertical(id="right"):
                yield Static("", id="pool")
                yield Static("GitHub PAT 检查中…", id="pat")
                yield Static("", id="rank-title")
                yield DataTable(id="rank")
        # THE REAL FOOTER IS THE BOTTOM BAR.  FooterKey is already clickable:
        # every entry below runs its action by simulating its binding key.
        # `key_display=""` keeps the labels clean without showing q/p/+/- keys.
        yield Footer(show_command_palette=False)

    async def on_mount(self) -> None:
        t = self.query_one("#vers", DataTable)
        t.add_columns("验证器", "状态", "当前任务", "预取队列",
                      "待确认分", "本次已确认", "已确认分", "无效",
                      "上传", "退回")
        t.cursor_type = "cell"
        r = self.query_one("#rank", DataTable)
        rank_cols = r.add_columns("名", "昵称", "当前积分", "分钟效率")
        for key, width in zip(rank_cols, (2, 10, 9, 8)):
            try:
                r.columns[key].width = width
            except Exception:
                pass
        r.cursor_type = "none"
        r.zebra_stripes = True
        self.fleet = Fleet(self.cfg, on_event=self._event,
                           on_config_changed=self._save_config)
        self._event("boot", "正在启动…")
        # PAINT FIRST, START LATER. `await self.fleet.start()` does a login, up
        # to sixteen claims and three engine launches — tens of seconds during
        # which nothing was drawn, which is a black terminal and no error.
        self.ready = False
        self._paint()
        self.set_interval(0.25, self._kick)
        self.set_interval(0.5, self._paint)
        self.set_interval(self._update_check_interval, self._check_updates)
        self.set_interval(self._update_check_interval, self._check_pat_status)
        self._startup = asyncio.create_task(self._startup_task())

    async def _startup_task(self) -> None:
        try:
            await self.fleet.start()
            self.cfg.update({k: self.fleet.cfg[k] for k in
                             ("verifiers", "active_verifiers", "base_verifiers",
                              "max_verifiers", "min_verifiers", "slots",
                              "lanes_per_slot", "cpu_limit_percent",
                              "known_engine", "wire_engine", "engine_exe", "claimed",
                              "client_protocol", "ahead_floor_tasks",
                              "ahead_remaining_ms", "pace_ms_default",
                              "riven_ahead_at",
                              "single_verifier_queue", "small_batch_upload",
                              "ui_scale_percent", "ui_scale_state",
                              "slot_watchdog_seconds",
                              "slot_idle_restart_seconds")
                             if k in self.fleet.cfg})
            config.save(self.cfg)
            self.ready = True
            try:
                self._apply_ui_scale_on_start()
            except Exception:
                pass
            self._event("boot", "就绪")
            self._announce_engine()
            asyncio.create_task(self._check_pat_status())
            asyncio.create_task(self._check_updates())
        except Exception as e:
            import traceback
            self._event("error", f"启动失败: {type(e).__name__}: {e}")
            try:
                with open(os.path.join(paths.ROOT, "error.log"), "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except Exception:
                pass
        finally:
            self._paint()

    def _kick(self) -> None:
        if not self.ready or self._inflight or not self.fleet or not self.fleet.running:
            return
        self._inflight = True
        asyncio.create_task(self._round())

    async def _round(self) -> None:
        try:
            await self.fleet.run()
        except Exception as e:
            self._event("error", f"loop: {type(e).__name__}: {e}")
        finally:
            self._inflight = False
            self.rounds += 1

    async def on_unmount(self) -> None:
        # A fallback: every exit path should have gone through action_shutdown,
        # but if the app is unmounted some other way the leases still go back.
        self._arm_shutdown_watchdog()
        if self.fleet and not self._closing:
            try:
                # Bounded: a fallback that hangs is worse than no fallback.
                await asyncio.wait_for(self.fleet.stop(), timeout=5)
            except Exception:
                pass

    async def action_shutdown(self) -> None:
        """Hand the work back to the server, then close.

        A lease we simply drop is a task nobody else can start until it lapses,
        so quitting reports it: board packs are released and a running riven is
        cancelled first (the engine would otherwise keep burning a core for a
        lease we no longer hold).
        """
        if self._closing:
            # A SECOND PRESS IS THE ESCAPE HATCH.  The first one can be in the
            # middle of a hand-back, and a TUI that silently ignores the second
            # press is indistinguishable from one that has hung.
            self._event("stop", "再次按下 q：立即退出，不再等待归还")
            os._exit(0)
        self._closing = True
        self.ready = False                 # stop new fetches at once
        self._arm_shutdown_watchdog(8.0)
        if self.fleet is None:
            self.exit()
            return
        self._event("stop", "正在向服务器归还任务…")
        try:
            await asyncio.wait_for(self.fleet.stop(), timeout=8)
            n = self.fleet.returned_on_stop
            self._event("stop", f"已归还 {n} 个租约，可以安全关闭"
                                if n else "没有持有的租约，可以安全关闭")
        except asyncio.TimeoutError:
            self._event("error", "归还超时（8 秒），直接退出")
        except Exception as e:
            self._event("error", f"归还失败：{type(e).__name__}: {e}，直接退出")
        finally:
            self.exit()

    async def action_quit(self) -> None:
        """Ctrl+C and Textual's own quit path go through the same hand-back."""
        await self.action_shutdown()

    # -------------------------------------------------------------- events
    def _save_config(self) -> None:
        config.save(self.cfg)

    def _maybe_stale_update(self, kind: str) -> None:
        # A stale response is actionable immediately.  Coalesce the several
        # slots that may hit it at once into one forced catalog/update check.
        if not (kind == "engine_stale" and self.ready and not self._updating
                and not self._engine_switch_lock.locked()
                and (self._stale_update_task is None
                     or self._stale_update_task.done())):
            return
        try:
            self._stale_update_task = asyncio.create_task(
                self._check_updates(force=True))
        except RuntimeError:
            pass

    def _event(self, kind: str, text: str) -> None:
        self._maybe_stale_update(kind)
        try:
            zh = {"boot": "启动", "login": "登录", "release": "退回",
                  "claim": "认领", "engines": "引擎", "fetch": "获取",
                  "fetch-ahead": "预取", "upload": "上传", "deadline": "截止",
                  "riven": "紫卡", "error": "错误", "pause": "暂停",
                  "update": "更新", "settings": "设置", "verifiers": "验证器",
                  "cores": "核心", "cpu": "CPU", "stop": "停止",
                  "engine_stale": "引擎过期", "refresh": "刷新",
                  "prefer": "调度", "claim_result": "认领结果",
                  "queue_promoted": "队列提升", "engine": "引擎",
                  "active": "常驻", "stale": "提前",
                  "refused": "跳过"}.get(kind, kind)
            self._write_log(f"[dim]{time.strftime('%H:%M:%S')}[/dim] "
                            f"[bold white on #d97706] {zh} [/] {text}")
            # ALSO PERSIST THE DIAGNOSTIC ONES.  The scrolling pane was the
            # only place these lived, so "why did it say that?" could not be
            # answered afterwards — a GitHub rate-limit warning left no trace
            # at all.  Only the low-volume kinds go to the file: fetch/upload
            # fire hundreds of times an hour and would double the log for
            # nothing.
            if self.fleet is not None and kind in _PERSIST_UI:
                self.fleet.log.event("ui", ui_kind=kind, text=text)
        except Exception:
            pass

    def _arm_shutdown_watchdog(self, seconds: float = 8.0) -> None:
        """Close the window after `seconds`, whatever the network does.

        The hand-back is best-effort: if a release or an engine stop will not
        come back, quitting must still quit.  This runs on a PLAIN THREAD, so it
        fires even when the event loop is the thing that is stuck — which is
        exactly the case that used to leave a frozen console behind.
        """
        if getattr(self, "_shutdown_watchdog", None) is not None:
            return
        import threading
        t = threading.Timer(seconds, _hard_exit)
        t.daemon = True
        t.start()
        self._shutdown_watchdog = t

    def _write_log(self, line: str) -> None:
        """Write a log line — but never yank the view if the reader scrolled up.

        A log that jumps to the newest line the moment anything happens cannot
        be read: it keeps the offset unless the view was ALREADY at the bottom,
        so history stays where the reader put it until they come back down.
        """
        log = self.query_one("#log", RichLog)
        at_bottom = True
        try:
            at_bottom = log.scroll_offset.y >= log.max_scroll_y - 1
        except Exception:
            pass
        try:
            log.auto_scroll = at_bottom
        except Exception:
            pass
        log.write(line)

    @staticmethod
    def _running_label(s: dict, paused: bool) -> str:
        if s.get("maintenance"):
            return "维护中"
        if paused:
            return "排空中" if s.get("tasks_total") else "已暂停"
        return "计算中"

    @staticmethod
    def _engine_label(s: dict) -> str:
        served = s.get("served_engine")
        local_engine = s.get("local_engine") or s.get("engine")
        effective = s.get("engine") or local_engine
        pin = s.get("wire_engine") or ""
        label = (f"[b]核心[/b] [b]{effective or '?'}[/b]"
                 f"（{local_engine or '?'}）")
        if pin and pin != effective:
            label += f" [yellow]手动 {pin} 已忽略[/yellow]"
        if served and served != local_engine:
            label += f" [dim]在线 {served} 待更新[/dim]"
        return label

    def _paint_header(self, s: dict, st: dict, paused: bool) -> None:
        uptime = int(s["uptime"])
        clock = f"{uptime // 3600}:{(uptime // 60) % 60:02d}:{uptime % 60:02d}"
        self.query_one("#head", Static).update(
            "[bold white on #d97706] WFSim 共享算力 [/] "
            f"[bold white on #d97706] {self._running_label(s, paused)} [/] "
            f"[b]已运行[/b] {clock}  "
            + self._engine_label(s)
            + "  "
            + f"[b]上传[/b] {st['uploaded']} "
            f"[dim](榜单 {st['board']} / 紫卡 {st['riven']})[/dim]  "
            f"[b]无效[/b] {st.get('task_invalid', 0)} "
            f"[dim](服务器 {st.get('task_gone', 0)} / 本地 "
            f"{st.get('task_local_invalid', 0)})[/dim]  "
            f"[b]退回[/b] {st.get('task_returned', 0)}")

    @staticmethod
    def _pool_account_text(s: dict) -> str:
        acct = s.get("account_points")
        if acct is not None:
            return f"[b]{acct}[/b]"
        if s.get("account_signed_in"):
            return "[green]已登录[/green] [dim](积分同步中)[/dim]"
        return "[dim]--[/dim] (未登录?)"

    @staticmethod
    def _pool_mode_text(s: dict) -> str:
        lanes = "/".join(str(x) for x in (s.get("slot_lanes") or [])) or "?"
        rslots = list(s.get("riven_slots") or [])
        if rslots:
            mode_s = f"紫卡 x{len(rslots)}  槽位 {rslots}"
        else:
            mode_s = f"榜单 lanes {lanes}"
        if s.get("drained_lanes"):
            stopped = s.get("stopped_slots") or []
            mode_s += (f"   [bold]{s['drained_lanes']} 线程已并入[/bold]"
                       f"（进程 {'/'.join(str(x + 1) for x in stopped)} 已释放）")
        return mode_s

    @staticmethod
    def _pool_renew_text(s: dict) -> str:
        if s.get("renew_supported") is None:
            return "[dim]未测试[/dim]"
        if s.get("renew_supported"):
            return "[green]已启用[/green]"
        return "[yellow]服务器不支持[/yellow]"

    def _paint_pool(self, s: dict, st: dict, f, paused: bool) -> None:
        pend, conf = f.session_points_now()
        rate = f.points_per_minute()
        rate_all = f.points_per_minute_all()
        rate_s = f"[b]{rate:.2f}[/b]/min" if rate is not None else "[dim]--[/dim]"
        rate_all_s = (f"[b]{rate_all:.2f}[/b]/min" if rate_all is not None
                      else "[dim]--[/dim]")
        acct_s = self._pool_account_text(s)
        mode_s = self._pool_mode_text(s)
        self.query_one("#pool", Static).update(
            "[bold white on #d97706] 贡献 [/]\n"
            f"[dim]账号积分[/dim] {acct_s}   [dim]本次上传[/dim] "
            f"[b]{st['uploaded']}[/b]\n"
            f"[dim]速度（近 5 分钟）[/dim]  {rate_s}\n"
            f"[dim]平均（启动至今）[/dim]   {rate_all_s}\n"
            f"[dim]本次待确认 / 本次已确认[/dim] {pend} / {conf}\n"
            "\n[bold white on #d97706] 任务池 [/]\n"
            f"[dim]紫卡 / 榜单[/dim] [b]{s['pool_riven']}[/b] / "
            f"[b]{s['pool_board']}[/b] 行\n"
            f"[dim]模式[/dim] {mode_s}\n"
            f"[dim]申请中[/dim] {s.get('fetching_count')}/2   "
            f"[dim]预取队列[/dim] {s['tasks_queued']}\n"
            f"[dim]持有[/dim] 计算中 {s['tasks_computing']} + 待分配 "
            f"{s['tasks_holding']} = [b]{s['tasks_total']}[/b] / "
            f"{s['tasks_capacity']}\n"
            "\n[bold white on #d97706] 资源 [/]\n"
            f"[dim]活跃验证器[/dim] {s['active']} / {s['max']} "
            f"（基础 {s['base']}）\n"
            f"[dim]核心[/dim] {f.core_pool_label()}\n"
            f"[dim]轮次[/dim] {self.rounds}   "
            f"[dim]续约[/dim] "
            + self._pool_renew_text(s)
            + ("\n[bold white on #d97706] 已暂停 [/]" if paused else ""))

    def _paint_rank(self, s: dict) -> None:
        lb = s.get("leaderboard") or []
        lb_at = float(s.get("leaderboard_at") or 0.0)
        err = str(s.get("leaderboard_error") or "")
        self.query_one("#rank-title", Static).update(
            "[bold white on #d97706] 贡献者排行 [/]"
            f" [dim]在线 {s.get('leaderboard_computing', 0)}/"
            f"{s.get('leaderboard_total', 0)} · 2分钟刷新 · [/dim]"
            f"{self._rank_freshness(lb_at, err)}")
        if lb_at == self._leaderboard_at:
            return
        r = self.query_one("#rank", DataTable)
        # REBUILD THE COLUMNS, AND PUT THE READER BACK.
        #
        # A column's width is its `content_width`, which the table only ever
        # GROWS — never shrinks — and `clear()` does not reset it.  So a leader
        # whose name was long once kept that column wide for the rest of the
        # session while every later name sat in a column far wider than it
        # needed.  Re-creating the columns re-measures them from the rows about
        # to be added, so the widths follow the names again in both directions.
        # `clear()` also parks the scroll offset at zero, which is what made a
        # 60-second refresh yank a reader back to the top; the offset is put
        # back after the next layout, once the new rows have been measured.
        keep_y = r.scroll_y
        r.clear(columns=True)
        rank_cols = r.add_columns("名", "昵称", "当前积分", "分钟效率")
        for key, width in zip(rank_cols, (2, 10, 9, 8)):
            try:
                r.columns[key].width = width
            except Exception:
                pass
        for row in lb:
            name = row.get("name")
            if not name or not str(name).strip():
                name = f"匿名 {str(row.get('key') or '')[:6]}"
            try:
                rank = max(0, int(row.get("rank") or 0))
                points = f"{int(row.get('points') or 0):,}"
                rate = str(max(0, int(round(float(row.get("rate") or 0)))))
            except Exception:
                rank, points, rate = 0, "0", "0"
            r.add_row(f"{rank:02d}", str(name), points, rate)
        # THE TIMESTAMP IS THE "PAINTED" MARK, taken only once the rows are in.
        # It used to be set before the rebuild, so a fault between the two left
        # it advanced over an empty table and every later tick returned early on
        # the equal timestamp — the ranking appeared to stop updating forever.
        # Taken here, a fault leaves it alone and the next tick retries.
        self._leaderboard_at = lb_at
        # AFTER THE NEXT LAYOUT, not now: the rows just added have not been
        # measured yet, so `max_scroll_y` is still the post-`clear()` zero and
        # an offset set here would be clamped away.
        self.call_after_refresh(self._restore_rank_scroll, keep_y)

    def _restore_rank_scroll(self, y: float) -> None:
        """Put the leaderboard reader back where they were, clamped to what is
        now there — a shorter leaderboard must not leave a dangling offset."""
        try:
            r = self.query_one("#rank", DataTable)
            r.scroll_y = min(float(y), r.max_scroll_y)
        except Exception:
            pass

    @staticmethod
    def _rank_freshness(lb_at: float, err: str) -> str:
        """How old the ranking on screen is, or why there is none.

        THE AGE IS THE HONEST ANSWER to "why is it not moving": upstream
        republishes the ranking every ten minutes (`stats_contributors_ms` in
        worker/dials.js, default and minimum 600 s) and serves the last file for
        up to twenty, so a reading that is minutes old with no error means the
        server has not written a new one — not that this client failed.

        The styling comes back WITH the text: the caller closes its `[dim]`
        span before this, because a red inside a dim reads grey — a failure has
        to look like one.
        """
        if err:
            # brackets out: rich reads them as markup and raises on an unclosed
            # tag, which would take the whole panel down for a log line
            short = err.replace("[", "(").replace("]", ")")[:28]
            # a CHIP, not red text: it is unmissable on any background, and it
            # reads as an error even where colour is lost (piped output, a
            # monochrome theme) because the block itself is the signal
            return f"[bold white on #b91c1c] 更新失败 {short} [/]"
        if lb_at <= 0.0:
            return "[dim]尚未取得[/dim]"
        age = max(0.0, time.time() - lb_at)
        if age < 5.0:
            return "[dim]刚刚更新[/dim]"
        if age < 60.0:
            return f"[dim]{int(age)} 秒前更新[/dim]"
        return f"[dim]{int(age // 60)} 分 {int(age % 60)} 秒前更新[/dim]"

    @staticmethod
    def _slot_idle_lines(st: dict, port: int, lanes: int, bar: str) -> list[str]:
        return [
            bar + f"[b] 引擎 {port} [/][dim]空闲[/dim]",
            "   [dim]等待可分配任务（当前无待分配的紫卡 / 榜单）[/dim]",
            f"   [dim]预取=--   上次 --   线程 {lanes}   完成 {st['done']}[/dim]",
            "   ",
            "   ",
        ]

    @staticmethod
    def _slot_progress(f, what: str, st: dict, prog: dict, elapsed: float,
                       riven: bool, lanes: int) -> tuple[str, str, str, str]:
        if riven:
            p = prog.get("pct")
            phase = PHASE_ZH.get(prog.get("phase"), prog.get("phase") or "?")
            pct_s = f"{p*100:3.0f}%" if p is not None else phase
            kind_zh = "紫卡"
            if prog.get("quick"):
                # QUICK: builds scored (`enumerated`) and fights run
                # (`sims_done`) are the live counts; the funnel's
                # `round`/`jobs`/`runs` are never set on this strategy.
                task_line = (f"任务 {what}   起点 {prog.get('starts', '?')}   "
                             f"阶段 {phase}   "
                             f"批次 {prog.get('enumerated', 0)}   "
                             f"战斗 {prog.get('sims_done', 0)}")
            else:
                task_line = (f"任务 {what}   起点 {prog.get('starts', '?')}   "
                             f"阶段 {phase}   "
                             f"轮次 {prog.get('round', '?')}/{prog.get('rounds', '?')}   "
                             f"批次 {prog.get('jobs', 0)}   战斗 {prog.get('runs', 0)}")
            planned = prog.get("sims_planned", 0)
            sims = (f"{prog.get('sims_done', 0)}/{planned}" if planned
                    else f"{prog.get('sims_done', 0)}")
            detail_line = (f"配装 {prog.get('enumerated', 0)}   "
                           f"模拟 {sims}   "
                           f"线程 {lanes}   完成 {st['done']}")
            return pct_s, kind_zh, task_line, detail_line
        p = prog.get("pct")
        if p is None:
            p = min(0.99, elapsed * 1000.0 / max(1, f.pace_ms))
            pct_s = f"~{p*100:3.0f}%"
        else:
            pct_s = f"{p*100:3.0f}%"
        kind_zh = "榜单"
        task_line = (f"任务 {what}   进度 {prog.get('done', 0)}/"
                     f"{prog.get('total', 0)}   耗时 {elapsed:.1f}s")
        detail_line = (f"行 {prog.get('done', 0)}/{prog.get('total', 0)}   "
                       f"线程 {lanes}   完成 {st['done']}")
        return pct_s, kind_zh, task_line, detail_line

    @staticmethod
    def _slot_prev_text(st: dict) -> str:
        if st["last_ok"] is None:
            return "[dim]上次 --[/dim]"
        mark = "成功" if st["last_ok"] else "失败"
        return f"上次 {st['last_verifier'][:8]} {mark} {st['last_ago']:.0f}s"

    @staticmethod
    def _slot_next_text(st: dict) -> str:
        if not st.get("next_lease"):
            return "预取=--"
        nleft = st.get("next_left_s")
        if nleft is None:
            return f"预取={st['next_label'][:24]}"
        return f"预取={st['next_label'][:20]} {nleft}s"

    @staticmethod
    def _slot_last_text(st: dict, riven: bool) -> str:
        last = st.get("last_counts") or {}
        if not last:
            return ""
        if riven:
            return (f"上轮 配装 {last.get('builds', 0)} "
                    f"战斗 {last.get('fights', 0)}")
        return (f"上轮 战斗 {last.get('runs', 0)} "
                f"射击 {last.get('shots', 0)}")

    @staticmethod
    def _slot_heartbeat_text(st: dict, vrec: dict, uploading: set) -> str:
        if not vrec:
            hb = "租约 --"
        else:
            hb = ""
            rn = int(vrec.get("renew_count") or 0)
            lf = vrec.get("left_s")
            lost = int(vrec.get("lost_rows") or 0)
            if rn:
                hb += f"续约 {rn}"
            if lf is not None:
                hb += f"   租约剩 {int(lf)}s"
            elif not hb:
                hb = "租约 --"
            if lost:
                hb += f"   丢行 {lost}"
        if st["verifier"] in uploading:
            hb += "   上传中"
        return hb

    def _slot_history(self, st: dict, riven: bool, vrec: dict,
                      uploading: set) -> tuple[str, str, str, str]:
        return (self._slot_next_text(st), self._slot_prev_text(st),
                self._slot_last_text(st, riven),
                self._slot_heartbeat_text(st, vrec, uploading))

    def _slot_active_lines(self, f, st: dict, vrec: dict, uploading: set,
                           lanes: int, bar: str) -> list[str]:
        who = st["verifier"][:10]
        what = st["label"][:30]
        if st.get("task_total") and st["task_total"] > 1:
            what += f" {st.get('task_index')}/{st['task_total']}"
        elapsed = st.get("elapsed") or 0.0
        prog = st.get("progress") or {}
        riven = (prog.get("kind") == "riven_gain"
                 or st.get("kind") == "riven_gain")
        pct_s, kind_zh, task_line, detail_line = self._slot_progress(
            f, what, st, prog, elapsed, riven, lanes)
        next_s, prev_s, last_s, hb = self._slot_history(
            st, riven, vrec, uploading)
        return [
            bar + f"[b] 引擎 {st['port']} [/]| {who} | {kind_zh} | "
                  f"已用 {elapsed:.0f}s | 进度 {pct_s}",
            f"   {task_line}",
            f"   {detail_line}",
            f"   {next_s}   {prev_s}" + (f"   {last_s}" if last_s else ""),
            f"   {hb}" if hb else "   ",
        ]

    def _slot_lines(self, f, s: dict, st: dict, by_vid: dict,
                    uploading: set) -> list[str]:
        no = int(st.get("slot", 0)) + 1
        port = st["port"]
        lanes = int(st.get("lanes") or 0)
        if not lanes:
            try:
                lanes = int((s.get("slot_lanes") or [])[no - 1])
            except Exception:
                lanes = 0
        bar = f"[bold white on #d97706] 计算进程 {no} [/]"
        if not st["verifier"]:
            return self._slot_idle_lines(st, port, lanes, bar)
        vrec = by_vid.get(st.get("verifier")) or {}
        return self._slot_active_lines(f, st, vrec, uploading, lanes, bar)
    def _paint_slots(self, s: dict, f) -> None:
        by_vid = {v["id"]: v for v in s["verifiers"]}
        uploading = set(s.get("uploading") or [])
        rows: list[str] = []
        for st in s["slots"]:
            rows.extend(self._slot_lines(f, s, st, by_vid, uploading))
        # The inner label, inside the scrolling pane: updating it does not touch
        # the pane's own scroll offset, so a reader's position survives.
        self.query_one("#slots-body", Static).update("\n".join(rows))

    @staticmethod
    def _verifier_state_cell(v: dict) -> str:
        # A VERIFIER SITTING OUT ITS RIVEN COOLDOWN IS NOT IDLE: it has work to
        # do in 70 s and is deliberately not asking until then, so say so
        # instead of "空闲" (which would read as "nothing to do").
        cooldown = int(v.get("riven_cooldown_s") or 0)
        if cooldown > 0:
            text = f"[bold white on #d97706] 冷却中 [/] [dim]{cooldown}s[/dim]"
            if v.get("active_forced"):
                text += " [bold white on #d97706] ★优先 [/]"
            return text
        # 备 IS A STATE, NOT A SUFFIX — when it is the whole story.
        #
        # A SPARE HOLDS WORK ONLY TO FINISH IT.  One standing down that still
        # owes rows reads `备·退` on its own: `待分配 备·退` was two chips
        # saying one thing.  One with nothing left — everything handed in and
        # not asking for more — reads a grey `备`: the grey already means idle,
        # so `空闲 备` said the same word twice.  A resident's `空闲` is
        # untouched, and a spare still doing a resident's job keeps the compact
        # marker, because there the reader does want to know which one it is.
        spare = bool(v.get("spare"))
        holding = bool(v.get("lease") or v.get("queued")
                       or v.get("inflight_tasks") or v.get("pending_tasks")
                       or v.get("tasks_ahead") or v.get("claim_pending"))
        if spare and v.get("retiring") and holding:
            text = "[bold white on #d97706] 备·退 [/]"
            if v.get("active_forced"):
                text += " [bold white on #d97706] ★优先 [/]"
            return text
        if spare and not holding:
            return "[dim]备[/dim]"
        spare_tag = " [bold white on #d97706] 备 [/]" if spare else ""
        # WAITING FOR A QUEUE IS STILL 计算中 AS FAR AS THIS COLUMN GOES.  The id
        # holds its pack and is working it (or has finished it and is waiting
        # for the next one); the reader's question — "is a second pack in hand?"
        # — is answered by the QUEUE column, which says 等待队列 and when the next
        # ask goes out.  A separate 等待中 chip here only made the row longer and
        # said the same thing twice.
        if v.get("queue_empty"):
            text = "[bold white on #d97706] 计算中 [/]"
            if v.get("active_forced"):
                text += " [bold white on #d97706] ★优先 [/]"
            return text + spare_tag
        state_zh = {"idle": "空闲", "fetching": "获取中", "holding": "待分配",
                    "computing": "计算中", "cooling": "冷却"}.get(v["state"], v["state"])
        if v["state"] == "idle":
            text = "[dim]空闲[/dim]"
        else:
            text = f"[bold white on #d97706] {state_zh} [/]"
        if v.get("banned_for_s"):
            text += f" [dim]{v['banned_for_s']}s[/dim]"
        if v.get("active_forced"):
            text += " [bold white on #d97706] ★优先 [/]"
        return text + spare_tag

    @staticmethod
    def _waiting_cell(ask_in_s: int | None) -> str:
        """THE PREFETCH COLUMN'S PLACEHOLDER while the queue is still empty.

        It sits where the task NAME would be, so the reader sees at a glance
        that this slot has nothing queued and why the row is still 计算中.  The
        countdown is the SERVER'S OWN delay (`next_ms`) — how long until this id
        may ask again.  Anything sooner is answered `early` and buys nothing, so
        a "seconds since the answers landed" figure would be a lie.
        """
        text = "等待队列"
        if ask_in_s:
            text += f" {int(ask_in_s)}s"
        return f"[dim]{text}[/dim]"

    @staticmethod
    def _work_cell(label: str | None, done: int, total: int,
                   left_s: int | None, computing: bool,
                   kind: str | None = None) -> str:
        """One slot, three segments: `名称 (完成/总) 租约剩`.

        The label drops its "pack 38x " / "board " / "riven " prefix — the
        bracket already says the same thing, so the cell reads like
        `torid (3/120) 60s`.  Orange on dark marks the work that is ON an
        engine right now (a computing pack or a running riven search).

        A RIVEN IS ONE QUESTION, NOT A PACK, so `(0/1)` would say nothing a
        reader wants: it shows the kind instead — `ocucor (riven) 60s`.  The
        count is kept for board rows, which is what "3 of 120 rows" is for.
        """
        if not label:
            return "[dim]—[/dim]"
        name = re.sub(r"^(pack \d+x |board |riven )", "", label)
        tail = "riven" if str(kind or "").startswith("riven") else f"{done}/{total}"
        left = f" {left_s}s" if left_s is not None else ""
        text = f"{name} ({tail}){left}"
        if computing:
            return f"[bold black on #d97706] {text} [/]"
        return text

    def _verifier_row(self, v: dict, f, by_slot: dict) -> tuple:
        sess = int(v.get("session_confirmed") or 0)
        invalid = int((v.get("task_counts") or {}).get("invalid") or 0)
        returned = int((v.get("task_counts") or {}).get("returned") or 0)
        return (
            f"{v['id'][:10]}",
            self._verifier_state_cell(v),
            # 正在计算 KEEPS THE YELLOW even when every row is done and the
            # answers are only waiting for a pack: the state column says 计算中
            # for exactly that case, and a cell that went plain would contradict
            # it.
            self._work_cell(v.get("work_label"), v.get("done_tasks", 0),
                            v.get("total_tasks", 0), v.get("left_s"),
                            bool(v.get("inflight_tasks"))
                            or bool(v.get("queue_empty")), v.get("kind")),
            # THE QUEUE COLUMN CARRIES THE WAIT.  With no queued pack it shows
            # the placeholder and the server's countdown instead of a task.
            (self._waiting_cell(v.get("ask_in_s"))
             if (v.get("queue_empty") and not v.get("queued_label"))
             else self._work_cell(v.get("queued_label"),
                                  v.get("queued_done", 0),
                                  v.get("queued_tasks", 0), v.get("queued_left_s"),
                                  bool(v.get("queued_inflight")), v.get("queued"))),
            (str(v["pending_points"]) if v["pending_points"] else "[dim]0[/dim]"),
            (f"[b]{sess}[/b]" if sess else "[dim]0[/dim]"),
            (str(v["confirmed_points"]) if v["confirmed_points"] else "[dim]0[/dim]"),
            (str(invalid) if invalid else "[dim]0[/dim]"),
            str(v["uploaded"]),
            (str(returned) if returned else "[dim]0[/dim]"),
        )

    def _paint_verifiers(self, s: dict, f) -> None:
        t = self.query_one("#vers", DataTable)
        try:
            t.styles.height = max(4, min(len(s["verifiers"]) + 2, 14))
        except Exception:
            pass
        by_slot: dict = {}
        for st in s["slots"]:
            if st["verifier"]:
                by_slot.setdefault(st["verifier"], []).append(st)
        # UPDATE IN PLACE, never `clear()`.  `clear()` drops every row and the
        # table rebuilds its scroll offset and cursor from scratch, so a reader
        # scrolled down (or sitting on a cell) was yanked back to the top on
        # every 0.5 s repaint.  Cells are rewritten under the same row key, so
        # the viewport, the selection and the column widths all stay put; rows
        # are only added or removed when the verifier set itself changes.
        want = [v["id"] for v in s["verifiers"]]
        have = [k.value for k in t.rows.keys()]
        for gone in [k for k in have if k not in want]:
            t.remove_row(gone)
        for v in s["verifiers"]:
            cells = self._verifier_row(v, f, by_slot)
            if v["id"] in t.rows.keys():
                for col, value in zip(t.columns.keys(), cells):
                    # `update_width=True` IS THE FIX FOR THE FROZEN COLUMNS.
                    # A cell written without it is never re-measured: the table
                    # only ever GROWS a column's content width, and it does that
                    # while adding a row — so an in-place repaint would leave
                    # 当前任务 / 预取队列 at whatever width the first paint
                    # happened to need.  With it, the changed cells are measured
                    # on the next idle and a column that got shorter is
                    # re-measured from the data it holds.
                    t.update_cell(v["id"], col, value, update_width=True)
            else:
                t.add_row(*cells, key=v["id"])

    def _paint(self) -> None:
        """Repaint every panel, EACH ON ITS OWN.

        ONE FAULT MUST NOT FREEZE THE REST: a painting error anywhere in here
        used to abort the whole of `_paint`, so every panel after the fault —
        the ranking is last — kept showing whatever it last painted.  A
        persistent fault therefore froze that panel for the rest of the
        session, which reads to the user as "the leaderboard stopped
        updating".  Each panel now catches its own error, and the failure is
        reported once per distinct message rather than twice a second.
        """
        try:
            f = self.fleet
            s = f.snapshot()
            st = s["stats"]
            paused = bool(f.paused)
        except Exception as e:
            self._paint_failed("snapshot", e)
            return

        def panel(name: str, paint) -> None:
            try:
                paint()
            except Exception as e:
                self._paint_failed(name, e)
            else:
                self._paint_errors.pop(name, None)

        panel("header", lambda: self._paint_header(s, st, paused))
        panel("pool", lambda: self._paint_pool(s, st, f, paused))
        panel("pat", lambda: self.query_one("#pat", Static).update(self._pat_panel_text()))
        panel("rank", lambda: self._paint_rank(s))
        panel("slots", lambda: self._paint_slots(s, f))
        panel("verifiers", lambda: self._paint_verifiers(s, f))

    def _paint_failed(self, panel: str, e: Exception) -> None:
        """Say which panel broke, once per distinct failure."""
        text = f"{type(e).__name__}: {e}"
        if self._paint_errors.get(panel) == text:
            return
        self._paint_errors[panel] = text
        try:
            self._event("error", f"paint {panel} failed: {text}")
        except Exception:
            pass
    # ------------------------------------------------------------- actions
    _PAT_STATIC = {
        "checking": "GitHub PAT 检查中…",
        "missing": "GitHub PAT 未配置（匿名请求 60/h）",
        "invalid": "GitHub PAT 无效 / 已过期（回退匿名）",
    }

    @staticmethod
    def _pat_expiry_text(days) -> str:
        if days is None:
            return "有效期未知"
        if days < 0:
            return "已过期"
        if days == 0:
            return "不足 1 天"
        return f"剩余 {days} 天"

    def _pat_panel_text(self) -> str:
        st = getattr(self, "_pat_state", None) or {"state": "checking"}
        state = st.get("state")
        if state in self._PAT_STATIC:
            return self._PAT_STATIC[state]
        if state == "ok":
            return f"GitHub PAT 有效 · {self._pat_expiry_text(st.get('days_left'))}"
        err = str(st.get("error") or "未知错误")
        return f"GitHub PAT 检查失败：{err[:40]}"

    async def _check_pat_status(self) -> None:
        if self.fleet is None:
            return
        self._pat_check_at = time.time()
        try:
            st = await self.fleet._io(sync_engine.pat_status)
        except Exception as e:
            st = {"state": "error", "error": f"{type(e).__name__}: {e}"}
        self._pat_state = st or {"state": "error", "error": "empty status"}
        self._paint()

    def _update_check_due(self, force: bool) -> bool:
        now = time.time()
        if (self._updating or self._engine_switch_lock.locked()
                or (self._auto_update_task is not None
                    and not self._auto_update_task.done())
                or self.fleet is None or not self.ready):
            return False
        if not force and now - self._update_check_at < self._update_check_interval - 60:
            return False
        # A FAILED UPDATE BACKS OFF, so one broken upstream cannot spin a
        # rebuild every ten minutes forever.
        if not force and now < self._update_retry_at:
            return False
        self._update_check_at = now
        return True

    @staticmethod
    def _catalog_ids(catalog: dict) -> tuple[list[str], list[str]]:
        reportable = [str(x.get("engine") or "")
                      for x in (catalog.get("reportable") or [])
                      if isinstance(x, dict)]
        ci_only = [str(x.get("engine") or "")
                   for x in (catalog.get("ci_only") or [])
                   if isinstance(x, dict)]
        return reportable, ci_only

    def _announce_catalog(self, reportable: list[str],
                          ci_only: list[str]) -> None:
        sig = (tuple(reportable), tuple(ci_only))
        if sig == self._catalog_sig:
            return
        self._catalog_sig = sig
        text = "版本目录 可切: " + " > ".join(reportable or ["无"])
        if ci_only:
            text += f" | CI待发布 {len(ci_only)}: " + ", ".join(ci_only[:6])
        self._event("engine", text)

    async def _read_engine_catalog(self) -> dict | None:
        # Signal 1: GitHub's switchable engine catalog, fetched first so a
        # temporary wfsim.app error cannot hide the version list.
        try:
            catalog = await self.fleet._io(sync_engine.github_engine_catalog)
        except Exception as e:
            self._event("update", f"GitHub 引擎目录检查失败，继续用当前列表：{e}")
            return None
        if not isinstance(catalog, dict):
            return None
        self._engine_catalog = catalog
        self.fleet.engine_catalog = catalog
        reportable, ci_only = self._catalog_ids(catalog)
        self._announce_catalog(reportable, ci_only)
        return catalog

    async def _read_release(self) -> dict | None:
        # Signal 2: the engine id the server validates against.
        try:
            rel = await self.fleet._io(self.fleet.remote.release_info)
        except Exception as e:
            self._event("update", f"engine 检查失败，下个周期重试：{e}")
            return None
        online = rel.get("engine")
        if not online:
            self._event("update", "release.json 里没有 engine 字段")
            return None
        self.fleet.served_engine = online
        return rel

    async def _read_local_update_state(self):
        try:
            state = await self.fleet._io(sync_engine.load_state) or {}
        except Exception:
            state = {}
        local_engine = (state.get("engine")
                        or self.fleet.cfg.get("known_engine") or None)
        self.fleet.local_engine = local_engine
        known_opt = state.get("optimizer_tree_sha")
        opt_sha = None
        try:
            # Signal 3: the riven search on public main.
            opt_sha = await self.fleet._io(sync_engine.optimizer_tree_sha)
        except Exception as e:
            # Rate limited, offline, GitHub down: keep running, try next time.
            self._event("update", f"optimizer/src 检查失败，下个周期重试：{e}")
        # Signal 4, LOCAL: this client's own patches.  The three above are the
        # server's, and none of them moves when a patch changes — without this
        # the fleet drive (or any later patch) would never reach an install that
        # is otherwise up to date.
        known_patches = state.get("patches_sha")
        try:
            cur_patches = await self.fleet._io(sync_engine.patches_sha)
        except Exception:
            cur_patches = None
        return local_engine, known_opt, opt_sha, known_patches, cur_patches

    @staticmethod
    def _catalog_wire_ids(catalog: dict, online: str):
        current = str(catalog.get("current") or online)
        accepted = {str(x.get("engine")): x
                    for x in (catalog.get("accepted") or [])
                    if isinstance(x, dict)}
        fallback = str(catalog.get("newest_accepted") or current)
        return current, accepted, fallback

    def _schedule_wire_switch(self, target: str, reason: str) -> None:
        if not self._updating:
            asyncio.create_task(self._switch_wire_engine(target, reason=reason))

    @staticmethod
    def _follow_wire_value(target: str, current: str | None) -> str:
        """Persist a wire-version choice: picking current means follow.

        A choice that was the latest when picked is stored as "" (follow the
        installed core), so the next release moves it instead of leaving a
        stale pin that the header then reports as ignored/expired.  A choice
        older than latest stays an explicit pin and keeps today's semantics.
        """
        target = str(target or "")
        if current and target == str(current):
            return ""
        return target

    def _wire_expired_switch(self, local_engine: str | None, current: str,
                             fallback: str, wire: str) -> bool:
        if local_engine == current:
            self._event("update", f"上报版本 {wire} 已过期，自动切到 {fallback}")
            self._schedule_wire_switch(fallback, "wire expired")
            return True
        # The installed core is itself behind.  Let the normal core update
        # replace it; the restart will read the fallback config.
        self.cfg["wire_engine"] = self._follow_wire_value(fallback, current)
        try:
            config.save(self.cfg)
        except Exception:
            pass
        return False

    def _handle_wire_version(self, catalog, online: str,
                             local_engine: str | None) -> bool:
        """Apply the wire-version policy; True when it needs no core update."""
        if not isinstance(catalog, dict):
            return False
        current, accepted, fallback = self._catalog_wire_ids(catalog, online)
        wire = str(self.fleet.wire_engine or self.fleet.engine_id
                   or local_engine or "")
        wire_ok = wire == current or wire in accepted
        # A stored pin that IS the current id was the latest when picked, not
        # a deliberate old pin: fold it into follow mode so the next release
        # moves it silently instead of warning as ignored/expired.  The
        # persisted config is cleared; the running value already reports the
        # same id, so nothing changes until the world moves.  A pin older
        # than current is left alone.
        explicit = str(self.cfg.get("wire_engine") or "").strip()
        if explicit and explicit == current:
            self.cfg["wire_engine"] = ""
            try:
                config.save(self.cfg)
            except Exception:
                pass
            self._event("engine", f"上报版本 {explicit} 已是最新，改为跟随最新")
        # A true `stale: true` means the server rejected the wire id even if a
        # just-refreshed catalog still lists it.  The server wins: drain and
        # move to the newest accepted id (or current).
        if self.fleet.maintenance and local_engine == current and wire_ok:
            self._event("update", f"服务器仍拒绝上报版本 {wire}，切到 {fallback}")
            self._schedule_wire_switch(fallback, "server rejected wire")
            return True
        if wire and not wire_ok:
            return self._wire_expired_switch(local_engine, current, fallback, wire)
        return False

    @staticmethod
    def _core_moved(online: str, local_engine: str | None,
                    known_opt, opt_sha: str | None) -> tuple[bool, bool]:
        engine_moved = bool(online) and online != local_engine
        opt_moved = bool(opt_sha) and known_opt != opt_sha
        return engine_moved, opt_moved

    def _announce_core_moves(self, engine_moved: bool, opt_moved: bool,
                             online: str, local_engine: str | None,
                             known_opt, opt_sha: str | None) -> None:
        if engine_moved:
            self._event("update",
                        f"engine 更新: 本地 {local_engine or '?'} -> 在线 {online}")
        if opt_moved:
            self._event("update",
                        f"optimizer 更新: {str(known_opt)[:12]} -> {opt_sha[:12]}")
        self._event("update", "开始同步并重建核心")

    def _start_core_update(self, rel: dict, online: str, local_engine: str | None,
                           known_opt, opt_sha: str | None, known_patches,
                           cur_patches, force: bool) -> None:
        engine_moved, opt_moved = self._core_moved(online, local_engine,
                                                   known_opt, opt_sha)
        patches_moved = bool(cur_patches) and known_patches != cur_patches
        wire = str(self.fleet.wire_engine or self.fleet.engine_id
                   or local_engine or "")
        if not engine_moved and not opt_moved and not patches_moved:
            if force:
                self._event("update", f"本地核心 {local_engine or '?'} = 在线 {online}，"
                                      f"上报 {wire or '?'}，optimizer = "
                                      f"{str(known_opt)[:12]}，补丁 = "
                                      f"{str(known_patches)[:12]}，已是最新")
            return
        if patches_moved and not engine_moved and not opt_moved:
            self._event("update", f"本地补丁已更新（{str(known_patches)[:12]} -> "
                                  f"{str(cur_patches)[:12]}），开始重编引擎")
        prebuilt = self._core_source == sync_engine.CORE_SOURCE_PREBUILT
        if prebuilt:
            # This line never syncs sources and never compiles: the served engine
            # has a published core, so the update is a download of those bytes.
            self._event("update", f"engine 更新: 本地 {local_engine or '?'} -> "
                                  f"服务器 {online}")
            self._event("update", "服务器已下发新引擎；后台下载中（计算不中断）")
            if not engine_moved:
                return
        else:
            self._announce_core_moves(engine_moved, opt_moved, online,
                                      local_engine, known_opt, opt_sha)
        reason = ("服务器已下发新引擎" if prebuilt else
                  ("本地补丁已更新" if patches_moved and not engine_moved
                   and not opt_moved else ""))
        if self._auto_update_task is None or self._auto_update_task.done():
            self._auto_update_task = asyncio.create_task(
                self._auto_update(rel, reason=reason))

    async def _is_prebuilt(self) -> bool:
        """True when this package can only DOWNLOAD a core, never build one.

        Asked of the package itself (`engine-src/state.json`), never inferred at
        update time: if a prebuilt zip could decide to rebuild, it would pause
        the fleet to run cargo that is not in the zip, fail on a missing
        core-host template, and retry forever (measured 2026-10-17).
        """
        if self._core_source is None:
            try:
                self._core_source = str(
                    await self.fleet._io(sync_engine.core_source) or "")
            except Exception:
                self._core_source = sync_engine.CORE_SOURCE_LOCAL
        return self._core_source == sync_engine.CORE_SOURCE_PREBUILT

    async def _check_updates(self, force: bool = False) -> None:
        """TWO LINES, TWO CHECKS.

        A LOCAL copy (this repo's sources + the bundled rust toolchain) follows
        FOUR SIGNALS every ten minutes:

          1. GitHub site/release.json + site/engines — the switchable list.
          2. wfsim.app/release.json engine — the served core id.
          3. the `optimizer/src` git tree SHA on public main — the riven
             search, which the engine id deliberately does not hash.
          4. THIS CLIENT'S PATCH SET — local, and the only one that moves when a
             patch changes.

        A PREBUILT copy (the CI zip: a compiled core, no toolchain) follows ONE
        signal and touches GitHub never: the served engine id.  See
        `_check_updates_prebuilt`.

        The served engine or optimizer moving, or a patch changing, means the
        core on disk is stale.  The wire id may be an older accepted id and is
        validated separately.
        """
        if not self._update_check_due(force):
            return
        if await self._is_prebuilt():
            await self._check_updates_prebuilt(force)
            return
        catalog = await self._read_engine_catalog()
        rel = await self._read_release()
        if rel is None:
            return
        online = rel.get("engine")
        (local_engine, known_opt, opt_sha, known_patches,
         cur_patches) = await self._read_local_update_state()
        if self._handle_wire_version(catalog, online, local_engine):
            return
        self._start_core_update(rel, online, local_engine, known_opt, opt_sha,
                                known_patches, cur_patches, force)

    async def _check_updates_prebuilt(self, force: bool) -> None:
        """The prebuilt line's whole periodic check: the served engine id.

        No GitHub, no optimizer tree, no local patch comparison.  A zip that
        ships a compiled core has nothing local to compare — its `patches_sha`
        is the shipped value while the tree has none, so comparing them reports
        a change that does not exist and would send the client off to compile
        sources it does not have.  The engine the server is serving is the whole
        signal; the published core for that id is a download.

        The engine catalog (a GitHub API call) is fetched only when the user
        actually opens the version list — see `_ensure_engine_catalog`.
        """
        rel = await self._read_release()
        if rel is None:
            return
        online = str(rel.get("engine") or "")
        state = await self.fleet._io(sync_engine.load_state) or {}
        local_engine = (state.get("engine")
                        or self.fleet.cfg.get("known_engine") or None)
        self.fleet.local_engine = local_engine
        if online and online != local_engine:
            self._start_core_update(rel, online, local_engine, None, None,
                                    None, None, force)
            return
        if force:
            self._event("update", f"本地核心 {local_engine or '?'} = "
                                  f"服务器 {online or '?'}，已是最新")

    def _engine_options(self) -> list[dict]:
        catalog = self._engine_catalog or (self.fleet.engine_catalog if self.fleet else None)
        if not isinstance(catalog, dict):
            return []
        return [x for x in (catalog.get("reportable") or []) if isinstance(x, dict)]

    def _engine_shift_target(self, delta: int) -> str | None:
        """Next reportable version, bounded by the installed binary's id."""
        if self.fleet is None:
            return None
        options = self._engine_options()
        ids = [str(x.get("engine") or "") for x in options]
        if not ids:
            return None
        wire = str(self.fleet.wire_engine or self.fleet.engine_id or "")
        if wire not in ids:
            return None
        try:
            current = ids.index(wire)
            local = ids.index(str(self.fleet.local_engine or ""))
        except ValueError:
            return None
        target = current + int(delta)
        if target < 0 or target > local:
            return None
        return ids[target]

    def _wire_target_allowed(self, target: str) -> bool:
        target = str(target or "").strip()
        if not target:
            return False
        ids = [str(x.get("engine") or "") for x in self._engine_options()]
        if target in ids:
            return True
        # The installed binary and the engine the server is currently serving\n        # (newest accepted) are always legitimate switch targets even when no\n        # catalog entry names them; this is what an automatic fallback relies on\n        # when the reportable list is empty right after a start.\n        if self.fleet is not None:
            for cand in (getattr(self.fleet, "local_engine", None),
                         getattr(self.fleet, "served_engine", None)):
                if cand and str(cand) == target:
                    return True
        return False

    async def _apply_wire_switch(self, f, target: str, old: str) -> bool:
        was_paused = bool(f.paused)
        old_effective = str(f.engine_id or f.wire_engine or "")
        new_effective = str(f._effective_engine(target) or "")
        # A pin on a machine whose installed core IS the served core does not
        # change what the server sees, so record it without draining a task.
        # Recording "current" means follow: the next release moves it.
        if new_effective == old_effective:
            stored = self._follow_wire_value(target, f.served_engine)
            f.wire_engine = stored
            self.cfg["wire_engine"] = stored
            try:
                config.save(self.cfg)
            except Exception:
                pass
            if not stored:
                self._event("engine", f"上报版本跟随最新（{target}），"
                                      f"实际上报 {new_effective}，无需切换")
            else:
                self._event("engine", f"已记录手动版本 {target}；本机运行当前引擎，"
                                      f"实际上报 {new_effective}，无需切换")
            return True
        try:
            f.maintenance = True
            await f.pause_drain()
            if not await self._wait_update_idle():
                raise TimeoutError("等待当前任务完成超时")
            f.wire_engine = target
            f.engine_id = f._effective_engine()
            self.cfg["wire_engine"] = self._follow_wire_value(target, f.served_engine)
            config.save(self.cfg)
            f.maintenance = False
            if not was_paused:
                await f.resume_from_pause()
            self._event("engine", f"上报版本已切换为 {f.engine_id}，任务已恢复")
            return True
        except Exception as e:
            f.wire_engine = old
            f.engine_id = f._effective_engine()
            f.maintenance = False
            if not was_paused and f.paused:
                try:
                    await f.resume_from_pause()
                except Exception:
                    pass
            self._event("error", f"上报版本切换失败，保留 {old}: {type(e).__name__}: {e}")
            return False

    async def _switch_wire_engine(self, target: str, reason: str = "manual") -> None:
        """Pause, drain, change only the wire id, then resume.

        This is deliberately not a core replacement.  The old implementation
        changed the id while leases were live, which could make an old lease's
        answer look as if it came from different bits.  Draining first removes
        that class of mismatch entirely.
        """
        if self.fleet is None or self._updating or self._engine_switch_lock.locked():
            return
        if not self._wire_target_allowed(target):
            self._event("error", f"不能切换到未允许的引擎版本 {target}")
            return
        async with self._engine_switch_lock:
            f = self.fleet
            old = str(f.wire_engine or f.engine_id or "")
            if not old or old == target:
                # The server may have rejected a version that the catalog now
                # says is current again.  Release the maintenance gate; no
                # drain is needed because the wire id is already the target.
                f.maintenance = False
                return
            self._event("engine", f"上报版本切换 {old} -> {target}（{reason}）：先暂停排空")
            await self._apply_wire_switch(f, target, old)

    async def _shift_wire_engine(self, delta: int) -> None:
        target = self._engine_shift_target(delta)
        if target:
            await self._switch_wire_engine(target, reason="manual")

    async def _set_wire_manual(self, enable: bool) -> None:
        """The 手动 switch beside the engine ± buttons.

        OFF (the default) means `cfg["wire_engine"]` is not read at all: the
        client reports the core it really installed.  ON means the id written in
        config.json applies, even when it is older than the served one, and the
        ± buttons become usable.  Flipping either way is a wire-id change, so it
        drains through the same path a shift does.
        """
        if self.fleet is None or self._updating or self._engine_switch_lock.locked():
            return
        enable = bool(enable)
        async with self._engine_switch_lock:
            f = self.fleet
            was_paused = bool(f.paused)
            old = str(f.engine_id or f.wire_engine or "")
            self.cfg["wire_manual"] = enable
            f.wire_manual = enable
            if enable:
                pinned = str(self.cfg.get("wire_engine") or "").strip()
                if not pinned:
                    # Nothing pinned yet: keep following the installed core, and
                    # leave the stored id empty so the ± buttons start from it.
                    f.wire_engine = str(f.local_engine or f.served_engine or "")
                    try:
                        config.save(self.cfg)
                    except Exception:
                        pass
                    self._event("engine", "手动版本上报：开；配置里还没有版本号，"
                                          "用 +/− 选一个后才会生效")
                    return
                self._event("engine", f"手动版本上报：开；读取配置里的 {pinned}")
                await self._apply_wire_switch(f, pinned, old)
                return
            # OFF: stop reading the file, report what is installed, drain if the
            # effective id actually changes.
            target = str(f.local_engine or f.served_engine or "")
            self.cfg["wire_engine"] = ""
            f.wire_engine = target
            new_effective = str(f._effective_engine() or "")
            try:
                config.save(self.cfg)
            except Exception:
                pass
            if new_effective == old or not new_effective:
                self._event("engine", "手动版本上报：关；上报版本跟随本机已安装核心")
                return
            try:
                f.maintenance = True
                await f.pause_drain()
                if not await self._wait_update_idle():
                    raise TimeoutError("等待当前任务完成超时")
                f.engine_id = new_effective
                f.maintenance = False
                if not was_paused:
                    await f.resume_from_pause()
                self._event("engine", f"手动版本上报：关；上报版本已切回 {new_effective}")
            except Exception as e:
                f.wire_engine = str(old or "")
                f.engine_id = old
                f.maintenance = False
                if not was_paused and f.paused:
                    try:
                        await f.resume_from_pause()
                    except Exception:
                        pass
                self._event("error", f"关闭手动版本失败，保留 {old}: {type(e).__name__}: {e}")

    async def _restart_fleet(self, engine_override: str | None = None) -> bool:
        """Bring the engines back up on whatever core is on disk right now."""
        try:
            self.fleet.maintenance = False
            await self.fleet.start()
            if engine_override:
                # ROLLED BACK: report the engine we are actually running.  The
                # id sent with every work/answer request must describe the bits
                # that computed the result, never what the server wishes we ran.
                self.fleet.engine_id = engine_override
                self.fleet.wire_engine = engine_override
                self.fleet.local_engine = engine_override
                self.cfg["known_engine"] = engine_override
                self.cfg["wire_engine"] = engine_override
                config.save(self.cfg)
            self.ready = True
            return True
        except Exception as e:
            self._event("error", f"启动引擎失败: {type(e).__name__}: {e}")
            return False

    def _restore_core_file(self) -> None:
        target = os.path.join(paths.ENGINE, "wfsim-core.exe")
        prev = os.path.join(paths.ENGINE, "wfsim-core.prev.exe")
        try:
            if os.path.exists(prev):
                shutil.copy2(prev, target)
                self._event("update", "已把老核心还原到 engine\\wfsim-core.exe")
            else:
                self._event("error", "没有找到 wfsim-core.prev.exe，无法回滚")
        except Exception as e:
            self._event("error", f"回滚老核心失败: {type(e).__name__}: {e}")

    def _restore_old_state(self, old_engine: str | None,
                           old_state: dict | None) -> None:
        if old_state is not None:
            try:
                sync_engine.save_state(old_state)
            except Exception as e:
                self._event("error", f"回滚 state.json 失败: {type(e).__name__}: {e}")
        if old_engine:
            self.cfg["known_engine"] = old_engine
            try:
                config.save(self.cfg)
            except Exception:
                pass

    async def _rollback_core(self, old_engine: str | None,
                             old_state: dict | None = None) -> None:
        """Put the previous core and state back, then start it again."""
        try:
            await self.fleet.stop()
        except Exception:
            pass
        self._restore_core_file()
        self._restore_old_state(old_engine, old_state)
        if not await self._restart_fleet(old_engine):
            self._event("error", "老核心也起不来；请查看 error.log")

    async def _run_sync_engine(self, *args: str) -> int:
        """Run sync_engine.py and stream its output into the update log."""
        sync = os.path.join(paths.HERE, "sync_engine.py")
        self._event("update", "运行更新器: " + " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-B", sync, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            self._event("update", line.decode("utf-8", "replace").rstrip())
        return await proc.wait()

    async def _wait_update_idle(self, timeout: float = 1900.0) -> bool:
        return await self.fleet.wait_idle(timeout)

    def _schedule_update_retry(self) -> None:
        delay = self._update_backoff
        self._update_backoff = min(1800.0, max(300.0, delay * 2.0))
        self._update_retry_at = time.time() + delay
        self._event("update", f"{delay / 60:.0f} 分钟后重试更新；期间照常运行")

    async def _auto_update(self, rel: dict, clean: bool = False,
                           manual: bool = False, reason: str = "") -> None:
        """Serialize core replacement with manual wire-version switching."""
        if self._engine_switch_lock.locked():
            if manual:
                self._event("update", "已有版本切换或核心更新正在进行，请稍后再试")
            else:
                self._event("update", "版本切换进行中；核心更新稍后重试")
                self._schedule_update_retry()
            return
        async with self._engine_switch_lock:
            await self._auto_update_locked(rel, clean=clean, reason=reason)

    async def _wait_build(self, build_task: asyncio.Task) -> None:
        deadline = time.time() + 1900.0
        while time.time() < deadline:
            if build_task.done():
                rc = build_task.result()
                if rc != 0:
                    raise RuntimeError(f"build-only 退出码 {rc}")
                return
            await asyncio.sleep(0.5)
        build_task.cancel()
        await asyncio.gather(build_task, return_exceptions=True)
        raise TimeoutError("构建新核心超时")

    async def _build_staged_core(self, clean: bool) -> None:
        build_args = ["--force", "--build-only"]
        if clean:
            build_args.append("--clean")
        build_task = asyncio.create_task(self._run_sync_engine(*build_args))
        try:
            await self._wait_build(build_task)
        except BaseException:
            if not build_task.done():
                build_task.cancel()
                await asyncio.gather(build_task, return_exceptions=True)
            raise

    def _announce_engine(self) -> None:
        """Say which core is running: the three are 2-3x apart and look alike.

        The site sees a result, not a build; a user who compiled a native core
        has to be told it is in force — and told when it silently fell back.
        """
        try:
            st = sync_engine.engine_status()
        except Exception:
            return
        nat = st.get("native") or {}
        if st.get("kind") == "native":
            extra = "PGO" if nat.get("pgo") else "无 PGO"
            self._event("engine", f"运行引擎：本机 native（{extra}，一般比通用版快 10%~15%）")
        elif st.get("kind") == "gnu":
            self._event("engine", "运行引擎：本地编译（GNU）")
        elif st.get("native_file"):
            if st.get("native_reason") == "cpu":
                # Copied to another machine: `-C target-cpu=native` binaries are
                # not portable, so this one is refused rather than risked.
                self._event("engine", "运行引擎：通用 v3（AVX2）；本机 native 引擎是为另一台"
                                      "设备（CPU 不同）编译的，已自动忽略")
            else:
                self._event("engine", "运行引擎：通用 v3（AVX2）；本机 native 引擎与服务器版本"
                                      "不一致，已自动回落")
        else:
            self._event("engine", "运行引擎：通用 v3（AVX2）")

    async def _native_build(self) -> None:
        """Compile the fastest engine for THIS machine and adopt it as the overlay.

        Everything except Visual Studio comes from the dependency pack, so this
        works on a prebuilt package too.  Computing continues while it compiles:
        `--build-native` only writes the overlay, and the one drain at the end
        happens after the build AND the anchor check have passed -- a core that
        fails either leaves the running engine untouched.
        """
        if self._engine_switch_lock.locked():
            self._event("update", "已有版本切换或核心更新正在进行，请稍后再试")
            return
        async with self._engine_switch_lock:
            if sync_engine.deps_pack() is None:
                self._event("error", "没有找到自编译依赖包，无法编译本机引擎")
                return
            self._event("update", "开始编译本机最快引擎（native + PGO），期间照常计算")
            rc = await self._run_sync_engine("--build-native")
            if rc != 0:
                self._event("error", f"本机引擎编译失败（退出码 {rc}）；继续用现有引擎")
                return
            self._event("update", "本机引擎已通过锚点校验；等计算进程空闲后启用")
            if not await self._wait_update_idle():
                self._event("error", "等待计算进程空闲超时；本机引擎下次启动时生效")
                return
            try:
                await self.fleet.stop()
            except Exception:
                pass
            if not await self._restart_fleet():
                self._event("error", "启用本机引擎失败；请查看 error.log")
                return
            st = sync_engine.engine_status()
            self._event("update", "本机引擎已启用" if st.get("kind") == "native"
                        else "本机引擎未生效，仍在用通用引擎")

    async def _install_and_restart(self, rel: dict) -> None:
        slots = self.fleet.slot_count()
        what = "整包已下载" if self._package_pending else "新核心已就绪"
        self._event("update", f"{what}；等待 {slots} 个计算进程收尾")
        if not await self._wait_update_idle():
            raise TimeoutError(f"等待 {slots} 个计算进程空闲超时")
        self._event("update", f"{slots} 个计算进程已空闲；关闭进程并替换核心")
        await self.fleet.stop()
        # Ensure every tracked engine process is actually gone before trying to
        # replace the binary on disk.
        for e in self.fleet.engines:
            if getattr(e, "proc", None) is not None and e.proc.poll() is None:
                try:
                    e.proc.kill()
                except Exception:
                    pass
                try:
                    e.proc.wait(timeout=10)
                except Exception:
                    pass
                e.proc = None
        if self._package_pending:
            rc = await self._run_sync_engine("--install-portable")
            if rc != 0:
                raise RuntimeError(f"整包替换退出码 {rc}")
            self._package_pending = False
            self._package_installed = True
            # The package's own engine-src/state.json is on disk now: adopt it
            # here so this window reports the new engine even when the relaunch
            # below fails.  data/config.json is deliberately kept (it holds the
            # user's settings), so known_engine has to be written by hand.
            try:
                st = await self.fleet._io(sync_engine.load_state) or {}
                if st.get("engine"):
                    self.cfg["known_engine"] = st["engine"]
                    self.cfg["engine_exe"] = "engine\\wfsim-core.exe"
                    config.save(self.cfg)
                    self.fleet.local_engine = st["engine"]
            except Exception:
                pass
            self._event("update", "整包已替换（客户端 + 引擎）；重启窗口以启用新版本")
            await self._relaunch()
            return
        rc = await self._run_sync_engine("--install-staged")
        if rc != 0:
            raise RuntimeError(f"安装 staged 核心退出码 {rc}")
        self.cfg["known_engine"] = rel.get("engine")
        self.cfg["engine_exe"] = "engine\\wfsim-core.exe"
        config.save(self.cfg)
        self._event("update", f"核心替换完成；用新核心重启 {self.fleet.slot_count()} 个计算进程")
        if not await self._restart_fleet():
            raise RuntimeError("新核心无法启动")

    async def _relaunch(self) -> None:
        """Start a fresh client from this folder, then quit this one.

        A whole-package update replaces src/*.py, which Python has already
        imported — the new client can only run in a new process.  It gets its own
        console (the same window the user would have opened by hand) and is then
        checked once: if it died immediately, this window stays so the error in
        logs/ is still visible.
        """
        # paths.ROOT is a str (see paths.py): dividing it with `/` raised
        # `TypeError: unsupported operand type(s) for /: 'str' and 'str'` AFTER a
        # whole-package update had already replaced src/ and engine/, which then
        # ran the old rollback and put the previous state.json back.
        flags = (int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
                 | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)))
        try:
            bat = os.path.join(paths.ROOT, "run-portable.bat")
            if os.path.isfile(bat):
                proc = subprocess.Popen(["cmd", "/c", bat],
                                        cwd=str(paths.ROOT), creationflags=flags)
            else:
                proc = subprocess.Popen([sys.executable, "src/app.py"],
                                        cwd=str(paths.ROOT), creationflags=flags)
        except Exception as e:                                       # noqa: BLE001
            self._event("error", f"自动重启失败（{type(e).__name__}: {e}）；请手动重开客户端")
            return
        await asyncio.sleep(3.0)
        if proc.poll() is not None:
            self._event("error", "新版本启动后立即退出（见 logs/error.log）；继续用当前窗口")
            return
        self._event("update", "新版本已在新窗口启动；这个窗口会自己关闭")
        await asyncio.sleep(1.0)
        self.exit()

    async def _resume_after_update(self, was_paused: bool, rel: dict) -> None:
        if was_paused:
            self.fleet.paused = True
            self._event("update", f"引擎 {rel.get('engine')} 就绪；保持用户暂停")
            return
        await self.fleet.resume_from_pause()
        self._event("update", f"引擎 {rel.get('engine')} 就绪；恢复接任务")

    async def _abort_update(self, old_engine, old_state: dict,
                            was_paused: bool) -> None:
        if self.fleet.running:
            self.fleet.maintenance = False
            if was_paused:
                self.fleet.paused = True
            else:
                await self.fleet.resume_from_pause()
            return
        await self._rollback_core(old_engine, old_state)

    async def _prepare_download(self, rel: dict, reason: str = "") -> None:
        """Prebuilt line: fetch the published package FIRST, drain only to install.

        Two shapes, in this order (the whole package is what the user asked for):
          1. the whole portable package — it carries the client sources too, so a
             server-side engine move brings the CLIENT forward as well;
          2. the core binary alone — the fallback for a fresh engine tag whose
             portable zip has not been published yet.

        Either way a download needs no idle slot, so the fleet keeps computing
        all the way through it and the pause-drain starts only once the bytes are
        staged — at which point the swap is the only thing left.
        """
        engine = str(rel.get("engine") or "")
        why = reason or "服务器已下发新引擎"
        self._event("update", f"{why}：下载整包 {engine}")
        rc = await self._run_sync_engine("--download-portable", engine)
        if rc == 0:
            self._package_pending = True
        else:
            self._package_pending = False
            self._event("update", f"整包暂不可用；改为只下载核心 {engine}")
            rc = await self._run_sync_engine("--download-core", engine)
            if rc != 0:
                raise RuntimeError(f"下载核心退出码 {rc}")
        self._event("update", "下载完成；进入暂停排空流程")
        self.fleet.maintenance = True
        await self.fleet.pause_drain()

    async def _prepare_update(self, clean: bool, reason: str = "") -> None:
        reason = reason or ("手动清理缓存重建引擎" if clean else "检测到核心更新")
        self._event("update", f"{reason}：进入暂停排空流程")
        self.fleet.maintenance = True
        await self.fleet.pause_drain()
        self._event("update", "已停止申请；后台开始构建新核心")
        await self._build_staged_core(clean)

    async def _finish_update(self, rel: dict) -> None:
        await self._install_and_restart(rel)
        self._update_backoff = 300.0
        self._update_retry_at = 0.0

    async def _auto_update_locked(self, rel: dict, clean: bool = False,
                                  reason: str = "") -> None:
        """Pause, build, swap only while idle, restart, resume.

        The old implementation called `drain_for_update()`, which stopped the
        three engines before starting cargo.  This one keeps the engines alive
        while the replacement is built, waits for every in-flight row/upload to
        finish, and only then closes the processes and atomically installs the
        staged binary.
        """
        if self._updating:
            return
        self._updating = True
        old_state = await self.fleet._io(sync_engine.load_state) or {}
        old_engine = (old_state.get("engine") or self.fleet.local_engine
                      or self.fleet.engine_id)
        was_paused = bool(self.fleet.paused)
        succeeded = False
        try:
            if self._core_source == sync_engine.CORE_SOURCE_PREBUILT:
                await self._prepare_download(rel, reason)
            else:
                await self._prepare_update(clean, reason)
            await self._finish_update(rel)
            succeeded = True
            # The pin may have tracked the just-replaced latest (or an auto
            # switch may have stored the new current explicitly): fold either
            # into follow mode.  A pin older than both is deliberate — keep it.
            stored = str(self.cfg.get("wire_engine") or "").strip()
            new_engine = rel.get("engine")
            if stored and (stored == old_engine
                           or self._follow_wire_value(stored, new_engine) == ""):
                self.cfg["wire_engine"] = ""
                try:
                    config.save(self.cfg)
                except Exception:
                    pass
            await self._resume_after_update(was_paused, rel)
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            self._event("error", f"核心更新失败: {type(e).__name__}: {e}")
            if self._package_installed:
                # src/ and engine/ are ALREADY the new package, so there is no
                # old tree to restore.  Rewriting state.json back — which is all
                # _rollback_core could actually do, since a package install
                # leaves no wfsim-core.prev.exe — made the folder disagree with
                # its own engine and every later check re-downloaded the whole
                # package.  Keep the new bookkeeping and just restart computing.
                self._event("update", "整包已替换到位；请手动重开客户端以启用新版本")
                await self._restart_fleet()
            else:
                await self._abort_update(old_engine, old_state, was_paused)
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self._updating = False
            if (not self.fleet.running and not succeeded
                    and not self._package_installed):
                # One final safety net: never leave the console with the
                # engines closed and no way back to the old core.
                try:
                    await self._rollback_core(old_engine, old_state)
                except Exception:
                    pass
            if not succeeded:
                self._schedule_update_retry()

    async def _manual_fast_rebuild(self) -> None:
        """Incremental rebuild: compile the current (patched) sources again."""
        await self._manual_rebuild(clean=False, reason="手动快速重新编译引擎")

    async def _manual_clean_rebuild(self) -> None:
        """Run a clean rebuild for this machine's CPU without changing versions."""
        await self._manual_rebuild(clean=True, reason="手动清理缓存重建引擎")

    async def _change_slots(self, delta: int) -> None:
        """The settings page's compute-process -/+.

        It goes through the fleet's own pause/drain/restart flow, so the count
        only moves between rows and the lanes are re-planned for it.
        """
        if self.fleet is None or not self.ready:
            self._event("error", "客户端尚未就绪，请稍后再调整计算进程数")
            return
        if (self._updating or self._engine_switch_lock.locked()
                or (self._auto_update_task is not None
                    and not self._auto_update_task.done())):
            self._event("update", "已有版本切换或核心更新正在进行，请稍后再试")
            return
        async with self._engine_switch_lock:
            current = self.fleet.slot_count()
            target = config.clamp_slots(current + int(delta))
            if target == current:
                return
            ok = await self.fleet.change_slot_count(target)
            self._paint()
            if ok:
                self._event("settings", f"计算进程 -> {self.fleet.slot_count()}")

    async def _manual_rebuild(self, clean: bool, reason: str) -> None:
        if not self.fleet or not self.ready:
            self._event("update", "客户端尚未就绪，请稍后再重建引擎")
            return
        if await self._is_prebuilt():
            # Honest answer instead of a five-minute pause that cannot succeed:
            # this package has a compiled core and no rust toolchain to compile
            # with.  The LOCAL line is still available when the dependency pack
            # is here -- that one needs a toolset, not this package's sources.
            if sync_engine.deps_pack() is not None:
                self._event("update", "本包没有源码树，无法从源码重建；"
                                      "但已放入自编译依赖包，可用右边的"
                                      "「编译本机最快引擎」编译 native 版本")
            else:
                self._event("update", "本包是预编译包（不含 rust 工具链与源码树），"
                                      "只能下载官方核心；把自编译依赖包解压到本文件夹后，"
                                      "可以编译本机最快引擎")
            return
        if (self._updating or self._engine_switch_lock.locked()
                or (self._auto_update_task is not None
                    and not self._auto_update_task.done())):
            self._event("update", "已有版本切换或核心更新正在进行，请稍后再试")
            return
        try:
            rel = await self.fleet._io(self.fleet.remote.release_info)
        except Exception as e:
            self._event("error", f"重建前读取 release.json 失败：{type(e).__name__}: {e}")
            return
        if not isinstance(rel, dict) or not rel.get("engine"):
            self._event("error", "release.json 没有 engine 字段，无法安全重建")
            return
        self._event("update", f"开始{reason}；完成后将自动重启引擎")
        await self._auto_update(rel, clean=clean, manual=True, reason=reason)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Click a status/queue cell to force the next pending row."""
        if getattr(event.data_table, "id", None) != "vers" or self.fleet is None:
            return
        column = int(event.coordinate.column)
        if column not in (1, 3):
            return
        row_key = getattr(event.cell_key, "row_key", None)
        vid = str(getattr(row_key, "value", row_key) or "")
        if not vid:
            return
        source = "active" if column == 1 else "queued"
        result = self.fleet.toggle_forced_task(vid, source)
        if not result:
            return
        action = result.get("action")
        where = "预取队列" if source == "queued" else "待分配"
        if action == "clear":
            self._event("prefer", f"已取消{where}强制优先：{result.get('label')}")
        else:
            self._event("prefer", f"已设为下次优先（{where}）："
                                    f"{result.get('verifier', '')[:10]} "
                                    f"{result.get('label')}")
        self._paint()
        event.stop()

    async def action_check_update(self) -> None:
        await self._check_updates(force=True)

    def _cores_changed(self, cpus: list[int]) -> None:
        self.fleet.cfg["engine_cores"] = list(cpus)
        self.cfg["engine_cores"] = list(cpus)
        self.fleet.apply_cores()
        try:
            self.fleet._plan_lanes()
        except Exception:
            pass
        self.fleet.apply_core_type()
        config.save(self.cfg)
        self._event("cores", f"引擎 CPU 池 -> {self.fleet.core_pool_label()}")

    def _cpu_limit_changed(self, percent: int) -> None:
        self.fleet.cfg["cpu_limit_percent"] = int(percent)
        self.cfg["cpu_limit_percent"] = int(percent)
        self.fleet.apply_cpu_limit()
        config.save(self.cfg)
        self._event("cpu", f"引擎 CPU 上限 -> {percent}%")

    def _core_type_changed(self, core_type: str) -> None:
        """P / max / E / auto for the engine threads.  Scheduling only, never maths."""
        self.fleet.cfg["core_type"] = str(core_type or "auto").lower()
        self.cfg["core_type"] = self.fleet.cfg["core_type"]
        try:
            self.fleet._plan_lanes()
        except Exception:
            pass
        config.save(self.cfg)
        self.fleet.apply_core_type()

    async def _refresh_verifiers(self) -> None:
        """MUST BE DONE AFTER COPYING THE CLIENT TO ANOTHER MACHINE.

        The ids in config are devices, and one device asking from two places gets
        its work to one and nothing to the other.
        """
        n = await self.fleet.refresh_verifiers()
        if n:
            self.cfg.update({"verifiers": self.fleet.cfg["verifiers"],
                             "max_verifiers": self.fleet.cfg["max_verifiers"],
                             "claimed": self.fleet.cfg["claimed"]})
            config.save(self.cfg)
            self._paint()

    async def _add_verifier(self) -> None:
        """Add one unclaimed verifier id for delayed claim.

        The id starts fetching before it is claimed; `Fleet._maybe_claim` claims
        it only after it owns both the first task and the `ahead:true` second
        one — which is what keeps a copied client from stealing the original's
        device identity.
        """
        f = self.fleet
        if not f or not f.running:
            return
        # A reduced verifier can still be alive while its last task uploads.
        # Re-activate that retained object instead of appending another one,
        # otherwise the table would jump from 3 back to 5 after one click.
        hard = int(f.cfg["max_verifiers"])
        if len(f.verifiers) > hard:
            f.cfg["max_verifiers"] = hard + 1
            self.cfg.update({"max_verifiers": f.cfg["max_verifiers"]})
            config.save(self.cfg)
            self._event("verifiers", f"恢复 {f.verifiers[hard].vid[:10]}"
                                     f" — 共 {hard + 1}")
            self._paint()
            return
        vid = await f.fresh_unclaimed_verifier()
        f.verifiers.append(Verifier(vid, label=f"v{len(f.verifiers)+1:02d}"))
        f.cfg["verifiers"] = [v.vid for v in f.verifiers]
        f.cfg["max_verifiers"] = len(f.verifiers)
        f.cfg["base_verifiers"] = int(f.cfg.get("base_verifiers") or 6)
        self.cfg.update({"verifiers": f.cfg["verifiers"],
                         "base_verifiers": f.cfg["base_verifiers"],
                         "max_verifiers": f.cfg["max_verifiers"],
                         "claimed": f.cfg.setdefault("claimed", [])})
        config.save(self.cfg)
        # a new id starts idle, so it fetches on the next round by itself
        self._event("verifiers", f"新增 {vid[:10]}（延迟认领）— 共 {len(f.verifiers)}")
        self._paint()

    async def _drop_verifier(self) -> None:
        """Stop using one verifier. It is NOT cancelled: a verifier that is
        computing right now finishes and uploads first, then simply never fetches
        again — its index is past the active count."""
        f = self.fleet
        if not f or not f.running:
            return
        hard = int(f.cfg["max_verifiers"])
        floor = max(1, int(f.cfg.get("min_verifiers") or 1))
        if hard <= floor:
            self._event("verifiers", f"已是下限（最少 {floor} 个）")
            return
        f.cfg["max_verifiers"] = hard - 1
        f.cfg["active_verifiers"] = min(int(f.cfg["active_verifiers"]), hard - 1)
        base = int(f.cfg.get("base_verifiers") or 3)
        f.cfg["base_verifiers"] = min(base, hard - 1)
        self.cfg.update({"max_verifiers": f.cfg["max_verifiers"],
                         "active_verifiers": f.cfg["active_verifiers"],
                         "base_verifiers": f.cfg["base_verifiers"]})
        config.save(self.cfg)
        busy = sum(1 for v in f.verifiers[hard-1:] if v.busy)
        self._event("verifiers", f"总数 -> {hard-1}"
                    + (f"（其中 {busy} 条正在收尾）" if busy else ""))
        self._paint()

    async def action_toggle_pause(self) -> None:
        """Pause = stop asking, hand back what has not started, finish what has.

        A row already on an engine is never interrupted, and a half-done pack
        sends the rows it finished and lets the rest go back — pausing must not
        throw progress away, and it must not take longer than the work in hand.
        """
        if not self.fleet:
            return
        if self.fleet.paused:
            await self.fleet.resume_from_pause()
        else:
            await self.fleet.pause_drain()

    async def action_more_active(self) -> None:
        if self.fleet:
            self.fleet.cfg["active_verifiers"] = min(
                int(self.fleet.cfg["max_verifiers"]),
                int(self.fleet.cfg["active_verifiers"]) + 1)
            self._event("active", f"常驻 -> {self.fleet.cfg['active_verifiers']}")

    async def action_less_active(self) -> None:
        if self.fleet:
            self.fleet.cfg["active_verifiers"] = max(
                1, int(self.fleet.cfg["active_verifiers"]) - 1)
            self._event("active", f"常驻 -> {self.fleet.cfg['active_verifiers']}")

    def action_tick_now(self) -> None:
        self._paint()

    async def action_add_verifier(self) -> None:
        await self._add_verifier()

    async def action_drop_verifier(self) -> None:
        await self._drop_verifier()

    async def action_refresh_verifiers(self) -> None:
        await self._refresh_verifiers()

    def action_open_cores(self) -> None:
        if self.fleet:
            self.push_screen(CoresScreen(self.fleet, self._cores_changed,
                                        self._cpu_limit_changed,
                                        self._core_type_changed))

    def _set_ui_scale(self, percent: int) -> None:
        """Apply the chosen terminal zoom and remember it.

        The TUI cannot scale its own glyphs, so this changes the current
        Windows Terminal profile's font size (with a one-time backup) and
        reports what happened; the choice itself is always kept.
        """
        percent = int(percent)
        if percent not in ui_scale.SCALE_CHOICES:
            percent = 0
        state = dict(self.cfg.get("ui_scale_state") or {})
        try:
            r = ui_scale.apply_scale(percent, state)
        except Exception as e:
            r = {"ok": False, "note": f"{type(e).__name__}: {e}"}
        self.cfg["ui_scale_percent"] = percent
        self.cfg["ui_scale_state"] = r.get("state", state)
        if self.fleet:
            self.fleet.cfg["ui_scale_percent"] = percent
            self.fleet.cfg["ui_scale_state"] = self.cfg["ui_scale_state"]
            self.fleet.on_event("settings",
                                f"界面缩放 {ui_scale.label(percent)}："
                                f"{r.get('note') or ''}")
        try:
            config.save(self.cfg)
        except Exception:
            pass

    def _apply_ui_scale_on_start(self) -> None:
        percent = int(self.cfg.get("ui_scale_percent") or 0)
        if percent != 0:
            self._set_ui_scale(percent)

    def _settings_engine_state(self) -> tuple[str, str, bool, bool]:
        if not self.fleet:
            return "?", "", False, False
        pin = str(self.fleet.wire_engine or "?")
        effective = str(self.fleet.engine_id or pin)
        local = str(self.fleet.local_engine or "?")
        served = str(self.fleet.served_engine or "?")
        catalog = self._engine_catalog or self.fleet.engine_catalog or {}
        ci = len(catalog.get("ci_only") or []) if isinstance(catalog, dict) else 0
        note = f"实际核心 {local} / 在线 {served}"
        if pin != effective:
            note += f" / 手动 {pin} 已忽略"
        if ci:
            note += f" / CI:{ci}"
        return (effective, note,
                self._engine_shift_target(-1) is not None,
                self._engine_shift_target(1) is not None)

    def action_open_settings(self) -> None:
        if self.fleet:
            self.push_screen(SettingsScreen(
                self.fleet, on_change=self._paint,
                engine_state=self._settings_engine_state,
                on_engine_shift=self._shift_wire_engine,
                on_engine_rebuild=self._manual_clean_rebuild,
                on_engine_rebuild_fast=self._manual_fast_rebuild,
                on_scale=self._set_ui_scale,
                on_slots=self._change_slots,
                on_engine_native=self._native_build,
                on_wire_manual=self._set_wire_manual))


def _cpu_rebuild_prompt() -> None:
    """If a core on disk was built for another CPU, say so and offer a rebuild.

    Two cores can be built for the wrong CPU, and they are not the same problem:
      * the GENERIC core is built for x86-64-v3 in CI, so it runs everywhere; the
        recorded CPU there is only a speed hint, and the record is refreshed;
      * the LOCAL native core is built with `-C target-cpu=native`, so it is
        machine-specific.  The whole portable folder can be copied to a second
        PC, so `sync_engine.native_active()` refuses an overlay whose recorded
        CPU is not this one, and this is where the user hears about it.

    What is offered depends on the line the package is on: a prebuilt package
    without a dependency pack has no toolchain and no sources, so a GNU rebuild
    would fail before it started -- the honest answer there is "keep using the
    generic core".  With a dependency pack the local line is `--build-native`,
    which only writes the overlay and therefore cannot lose the fallback.
    """
    try:
        state = sync_engine.load_state() or {}
        recorded = str(state.get("cpu") or "").strip()
        current = cores.cpu_model().strip()
        nat = state.get("native") or {}
        nat_cpu = str(nat.get("cpu") or "").strip() if isinstance(nat, dict) else ""
        has_native = sync_engine.NATIVE_CORE.exists()
        prebuilt = sync_engine.core_source() == sync_engine.CORE_SOURCE_PREBUILT
        pack = sync_engine.deps_pack() is not None
    except Exception:
        return
    if not current:
        return
    if not recorded:
        try:
            state["cpu"] = current
            sync_engine.save_state(state)
        except Exception:
            pass
        recorded = current
    stale_native = bool(has_native and nat_cpu and nat_cpu != current)
    if recorded == current and not stale_native:
        return
    print()
    if stale_native:
        print("本机 native 引擎不是为这台设备编译的，已自动忽略（回落通用引擎）：")
        print(f"  native 编译时的 CPU: {nat_cpu}")
        print(f"  当前设备的 CPU     : {current}")
    if recorded != current:
        print("检测到通用引擎不是为这台设备编译的：")
        print(f"  引擎编译时的 CPU: {recorded}")
        print(f"  当前设备的 CPU  : {current}")
        print("通用引擎是 x86-64-v3（AVX2）产物，可以照常使用；"
              "想要本机最快速度可以按当前 CPU 重新编译。")
    if prebuilt and not pack:
        print("本包是预编译包（不含工具链与源码）：继续使用通用引擎即可。"
              "想在这台机器上编译最快引擎，请把自编译依赖包解压进本目录。")
        return
    script = os.path.join(paths.HERE, "sync_engine.py")
    if pack:
        print("本包带自编译依赖包：可以编译本机最快引擎（native + PGO）。")
        try:
            answer = input("是否现在编译本机最快引擎？[y/N] ").strip().lower()
        except Exception:
            answer = ""
        if answer not in ("y", "yes"):
            print("已选择否：直接进入主界面，之后可在设置页点“编译本机最快引擎”。")
            return
        print("开始编译，通常需要 2~4 分钟；期间照常计算，请不要关闭窗口。")
        rc = subprocess.call([sys.executable, "-B", script, "--build-native"])
        if rc != 0:
            print(f"编译失败（退出码 {rc}），继续使用现有引擎。")
            return
        print("编译完成，本机最快引擎已启用。")
        return
    try:
        answer = input("是否现在不用缓存、按当前 CPU 重新编译引擎？[y/N] ").strip().lower()
    except Exception:
        answer = ""
    if answer not in ("y", "yes"):
        print("已选择否：直接进入主界面，继续使用现有引擎。")
        return
    print("开始清理缓存并重新编译，通常需要几分钟；期间请不要关闭窗口。")
    for args in (["--force", "--build-only", "--clean"], ["--install-staged"]):
        rc = subprocess.call([sys.executable, "-B", script, *args])
        if rc != 0:
            print(f"重建失败（退出码 {rc}），继续使用现有引擎。")
            return
    print("重新编译完成，使用新引擎进入主界面。")


def _pin_log_cpu(cfg) -> None:
    """Give this process its own logical processor before anything heavy runs.

    The client's loop, thread pools and logger share one home core chosen by
    the log-task policy (the least performant core; one SMT thread when the
    core has siblings).  Everything else — including the sibling of an SMT
    core — stays in the engine pool, so a busy Python always has a thread and
    an idle one leaves the whole core to the engines.  A failure here is not
    fatal: the client simply runs on every core as before.
    """
    try:
        import cores
        plan = cores.apply_log_cpu_policy(cfg)
    except Exception as e:
        print(f"日志任务 CPU 亲和性：设置失败（{type(e).__name__}: {e}），"
              f"客户端随系统调度。")
        return
    if plan:
        print(f"日志任务 CPU 亲和性：{cores.log_cpu_label(plan)}")
    elif str((cfg or {}).get("log_cpu", "auto")).lower() in ("off", "none",
                                                              "false", "0"):
        print("日志任务 CPU 亲和性：已在配置中关闭。")


def _raise_self_priority() -> None:
    """One priority class above normal, so engine threads never precede us.

    The engines run BELOW_NORMAL (api.py), which already means they only get
    CPU the client is not using; this makes the relationship explicit from
    the client's side too — its loop, TLS handshakes and log writes win every
    tie against compute work.  A failure is cosmetic: scheduling continues
    at the default class.
    """
    try:
        import cores
        if cores.set_self_priority(cores.ABOVE_NORMAL_PRIORITY_CLASS):
            print("进程优先级：客户端 高于标准；计算引擎 低于标准。")
        else:
            print("进程优先级：设置失败（继续使用默认优先级）。")
    except Exception as e:
        print(f"进程优先级：设置失败（{type(e).__name__}: {e}）。")


if __name__ == "__main__":
    # DOUBLE-CLICKED, THIS IS THE ONLY CHANCE TO SAY WHAT WENT WRONG: a console
    # that closes on the traceback is a program that "just does not start".
    import os as _os
    import traceback as _tb
    try:
        cfg, created = config.load_or_create()
        if created:
            raise SystemExit(
                "未找到 data/config.json，已按当前设备生成默认配置。\n"
                "请先查看 README.txt，填写 account.email、account.password；\n"
                "github_pat 可以留空，但建议填写以提高更新检查额度。\n"
                "填写完成后重新启动客户端。")
        if not (cfg.get("account") or {}).get("email"):
            raise SystemExit(
                "data/config.json 尚未填写 account.email / account.password。\n"
                "请查看 README.txt 的“首次配置 data/config.json”一节，填写后重新启动。")
        _pin_log_cpu(cfg)
        _raise_self_priority()
        _cpu_rebuild_prompt()
        WfSimClient(cfg).run()
    except SystemExit as e:
        if e.code:
            print(e.code)
            input("\n按回车关闭…")
        raise
    except BaseException:
        text = _tb.format_exc()
        try:
            with open(_os.path.join(paths.ROOT, "error.log"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        print(text)
        print("（已写入 error.log）")
        input("\n按回车关闭…")
        raise
