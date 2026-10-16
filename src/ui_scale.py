# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""Manual TUI zoom, applied to the TERMINAL, not to Textual.

A terminal application cannot scale its own glyphs: the cell size belongs to
the terminal host.  On Windows the app can change two things:

  * the current Windows Terminal profile's `font.size` (WT watches its
    `settings.json` and applies the change live), and
  * a classic console's font, via `SetCurrentConsoleFontEx`.

Everything here is best-effort and reversible.  A failure only reports a note;
the config value stays, so the choice is remembered.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import time
from ctypes import wintypes

#: 0 means "follow the system"; the rest are the menu's percentages.
SCALE_CHOICES = (0, 50, 75, 100, 125, 150, 175, 200)
FOLLOW_LABEL = "跟随系统"
MIN_FONT, MAX_FONT = 4.0, 72.0


def label(percent: int) -> str:
    return FOLLOW_LABEL if int(percent) == 0 else f"{int(percent)}%"


def system_scale() -> float:
    """Windows' effective scale for this window, e.g. 1.5 at 150%.

    GetDpiForMonitor is asked first because it reports the monitor the console
    is really on; GetDpiForSystem is the fallback, and 1.0 the last resort.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        shcore = ctypes.WinDLL("shcore")
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        hwnd = kernel32.GetConsoleWindow()
        mon = user32.MonitorFromWindow(hwnd, 2)      # NEAREST
        dx = ctypes.c_uint()
        dy = ctypes.c_uint()
        if shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            dpi = int(dx.value) or int(dy.value) or 96
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetDpiForSystem.restype = ctypes.c_uint
        return max(1.0, float(user32.GetDpiForSystem()) / 96.0)
    except Exception:
        return 1.0


def _settings_paths() -> list[str]:
    local = os.environ.get("LOCALAPPDATA") or ""
    cands = []
    for pkg in ("Microsoft.WindowsTerminal_8wekyb3d8bbwe",
                "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"):
        cands.append(os.path.join(local, "Packages", pkg, "LocalState",
                                  "settings.json"))
    cands.append(os.path.join(local, "Microsoft", "Windows Terminal",
                              "settings.json"))
    cands.append(os.path.join(local, "Microsoft", "Windows Terminal Preview",
                              "settings.json"))
    return [p for p in cands if os.path.isfile(p)]


def _brace_pairs(text: str) -> dict:
    """{position: matching close} for every brace outside strings/comments."""
    pairs: dict = {}
    stack: list = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        elif ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                pairs[stack.pop()] = i
        i += 1
    return pairs


def _profile_span(text: str, guid: str):
    """The `{...}` of the profile whose guid is `guid`, or None."""
    m = re.search(r'"guid"\s*:\s*"' + re.escape(guid) + r'"', text, re.I)
    if not m:
        return None
    best = None
    for start, end in _brace_pairs(text).items():
        if start < m.start() < end and (best is None or start > best[0]):
            best = (start, end)
    return best


def _default_profile_guid(text: str) -> str:
    m = re.search(r'"defaultProfile"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _span_font_size(text: str, start: int, end: int) -> float | None:
    span = text[start:end + 1]
    m = re.search(r'"size"\s*:\s*([0-9]+(?:\.[0-9]+)?)', span)
    return float(m.group(1)) if m else None


def _set_font_size(text: str, start: int, end: int, size: float) -> str:
    """One profile's `font.size`, changed without touching the rest of the file."""
    span = text[start:end + 1]
    mf = re.search(r'"font"\s*:\s*\{', span)
    if mf:
        fopen = start + mf.end() - 1
        pairs = _brace_pairs(text)
        fclose = pairs.get(fopen)
        if fclose is None or fclose > end:
            raise ValueError("font object not closed inside the profile")
        fspan = text[fopen:fclose + 1]
        inner = fspan[1:-1]
        msize = re.search(r'"size"\s*:\s*[0-9]+(?:\.[0-9]+)?', inner)
        if msize:
            inner = (inner[:msize.start()] + f'"size": {size:g}'
                     + inner[msize.end():])
        else:
            sep = "" if inner.strip() in ("", ",") else ","
            inner = f'"size": {size:g}{sep}' + inner
        return text[:fopen] + "{" + inner + "}" + text[fclose + 1:]
    # No font object: add one before the profile's closing brace.
    inner = span[:-1]
    sep = "" if inner.rstrip().endswith(("{", ",")) else ","
    new_span = inner + f'{sep}\n                "font": {{ "size": {size:g} }}\n            }}'
    return text[:start] + new_span + text[end + 1:]


def _valid_jsonc(text: str) -> bool:
    """Cheap sanity check: strip comments/trailing commas, then parse."""
    out, in_str, i, n = [], False, 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1:i + 2])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(ch)
        i += 1
    clean = re.sub(r",\s*([}\]])", r"\1", "".join(out))
    try:
        json.loads(clean)
        return True
    except Exception:
        return False


def _backup(path: str) -> None:
    bak = path + ".wfsim-backup"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)


def _write_atomic(path: str, text: str) -> None:
    tmp = path + f".wfsim-{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _apply_wt(percent: int, state: dict) -> dict | None:
    """Change the current WT profile's font.size; None when WT is not here."""
    paths = _settings_paths()
    if not paths:
        return None
    path = paths[0]
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    guid = (os.environ.get("WT_PROFILE_ID") or "").strip()
    if not guid:
        guid = _default_profile_guid(text)
    span = _profile_span(text, guid) if guid else None
    if span is None and guid:
        # Some sessions set WT_SESSION but not WT_PROFILE_ID; the default
        # profile is the best available answer.
        guid = _default_profile_guid(text)
        span = _profile_span(text, guid) if guid else None
    if span is None:
        return {"ok": False, "note": "找不到当前 Windows Terminal 配置档",
                "state": state}

    base = state.get("wt_base_font")
    if state.get("wt_base_guid") != guid or base is None:
        base = _span_font_size(text, *span)
        if base is None:
            base = 12.0
        state = dict(state, wt_base_guid=guid, wt_base_font=base,
                     wt_base_sys=system_scale())
    if percent == 0:
        size = float(base)
    else:
        size = float(base) * (percent / 100.0) / max(0.25, system_scale())
        size = max(MIN_FONT, min(MAX_FONT, round(size * 2) / 2))
    new_text = _set_font_size(text, span[0], span[1], size)
    if not _valid_jsonc(new_text):
        return {"ok": False, "note": "缩放写回会破坏配置，已放弃",
                "state": state}
    _backup(path)
    _write_atomic(path, new_text)
    return {"ok": True, "note": f"字号 {size:g}（{label(percent)}）",
            "path": path, "state": state}


def apply_scale(percent: int, state: dict | None = None) -> dict:
    """Apply one scale choice; returns the new state and a user-facing note."""
    state = dict(state or {})
    try:
        percent = int(percent)
    except Exception:
        percent = 0
    if percent not in SCALE_CHOICES:
        percent = 0
    out = _apply_wt(percent, state)
    if out is None:
        out = {"ok": False, "note": "当前终端不是 Windows Terminal，无法自动缩放"}
    out["percent"] = percent
    out.setdefault("state", state)
    return out
