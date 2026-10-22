# -*- coding: utf-8 -*-
#---------------------------------------------------------------------------
# wfclient - a fork of WFSim (https://github.com/magenie33/wfsim).
# Licensed AGPL-3.0-or-later, same as upstream. The changes this fork makes
# are listed in SECOND-DEVELOPMENT.md; attribution and naming: NOTICE.md.
#---------------------------------------------------------------------------
"""The CPU cores this machine has, by the names Windows gives them, and the
affinity mask that decides which of them the engines may use."""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetLogicalProcessorInformationEx.argtypes = [
    wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
k32.GetLogicalProcessorInformationEx.restype = wintypes.BOOL
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
k32.SetProcessAffinityMask.restype = wintypes.BOOL
k32.GetProcessAffinityMask.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_size_t)]
k32.GetProcessAffinityMask.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
k32.SetInformationJobObject.restype = wintypes.BOOL
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k32.AssignProcessToJobObject.restype = wintypes.BOOL
k32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
k32.QueryInformationJobObject.restype = wintypes.BOOL
# --- CPU Sets: the mechanism Microsoft actually supports for P/E steering on a
# --- hybrid part.  A process-level AFFINITY MASK is the older, coarser thing.
k32.GetSystemCpuSetInformation.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    wintypes.HANDLE, wintypes.DWORD]
k32.GetSystemCpuSetInformation.restype = wintypes.BOOL
k32.SetProcessDefaultCpuSets.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
k32.SetProcessDefaultCpuSets.restype = wintypes.BOOL
# --- read back what is actually in force, for the detailed log only.  A plan
# --- that was never applied and one that was look the same in config.
k32.GetProcessDefaultCpuSets.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
k32.GetProcessDefaultCpuSets.restype = wintypes.BOOL
k32.SetThreadSelectedCpuSets.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
k32.SetThreadSelectedCpuSets.restype = wintypes.BOOL
k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenThread.restype = wintypes.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.SetProcessInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
k32.SetProcessInformation.restype = wintypes.BOOL
k32.GetProcessTimes.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME)]
k32.GetProcessTimes.restype = wintypes.BOOL
k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.SetPriorityClass.restype = wintypes.BOOL
k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
k32.GetPriorityClass.restype = wintypes.DWORD

RelationProcessorCore = 0
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4


class _CpuRateInfo(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class CpuRateLimit:
    """A Windows Job Object hard CPU cap for one engine process.

    CpuRate is expressed in hundredths of a percent: 10000 is 100% of the
    whole machine's logical processors.  The cap is applied to the process,
    so the Rust engine keeps its exact same work order and output; only how
    often Windows schedules its threads changes.
    """

    def __init__(self, percent: int = 100):
        self.job = None
        self.assigned_pids: set[int] = set()
        self.percent = self._clamp(percent)
        self.error = ""

    @staticmethod
    def _clamp(percent: int) -> int:
        try:
            return max(10, min(100, int(percent)))
        except Exception:
            return 100

    def _ensure_job(self) -> bool:
        if self.job:
            return True
        h = k32.CreateJobObjectW(None, None)
        if not h:
            self.error = f"CreateJobObject failed ({ctypes.get_last_error()})"
            return False
        self.job = h
        return True

    def set_percent(self, percent: int) -> bool:
        """Set or update the hard cap. Returns False on Windows API failure."""
        self.percent = self._clamp(percent)
        if not self._ensure_job():
            return False
        info = _CpuRateInfo(
            JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
            self.percent * 100)
        ok = k32.SetInformationJobObject(
            self.job, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            self.error = f"SetInformationJobObject failed ({ctypes.get_last_error()})"
            return False
        self.error = ""
        return True

    def assign(self, pid: int) -> bool:
        """Put a running process under this job. Safe to call repeatedly."""
        if not self._ensure_job():
            return False
        if int(pid) in self.assigned_pids:
            return True
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not h:
            self.error = f"OpenProcess failed ({ctypes.get_last_error()})"
            return False
        try:
            ok = bool(k32.AssignProcessToJobObject(self.job, h))
            if not ok:
                self.error = f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
                return False
            self.assigned_pids.add(int(pid))
            self.error = ""
            return True
        finally:
            k32.CloseHandle(h)

    def actual_percent(self) -> int | None:
        """What Windows is ACTUALLY enforcing, not what we asked for.

        The console used to print the requested number from config, so a job
        that refused the setting looked exactly like a job that took it.
        """
        if not self.job:
            return None
        info = _CpuRateInfo()
        got = wintypes.DWORD(0)
        ok = k32.QueryInformationJobObject(
            self.job, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(got))
        if not ok:
            return None
        if not (info.ControlFlags & JOB_OBJECT_CPU_RATE_CONTROL_ENABLE):
            return 100
        return int(info.CpuRate) // 100

    def close(self) -> None:
        if self.job:
            k32.CloseHandle(self.job)
        self.job = None
        self.assigned_pids.clear()




def _filetime_ticks(value) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def process_cpu_seconds(pid: int) -> float | None:
    """Total kernel+user CPU seconds of one process, or None when unreadable.

    THE ONE HONEST WAY TO TELL "STILL COMPUTING" FROM "STUCK": a running engine
    always accumulates CPU time, a wedged one stops.  A board row has no
    progress channel of its own, so a slot watchdog needs this instead of a
    "long enough" guess.  Windows reports it in 100 ns ticks.
    """
    if not pid:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                 ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        return (_filetime_ticks(kernel) + _filetime_ticks(user)) / 1e7
    finally:
        k32.CloseHandle(h)


# --- PER-CORE AND PER-THREAD CPU: who else was on the machine ---------------
# A row that got slow is either a heavier row or a row whose cores were taken.
# Process CPU seconds cannot tell those apart; these two can.
ntdll = ctypes.WinDLL("ntdll")
_SystemProcessorPerformanceInformation = 8
_core_times_buf = None
_core_times_n = 0


def system_core_times() -> list[tuple[int, int, int]] | None:
    """(idle, kernel, user) 100 ns ticks per logical core, or None.

    ONE NtQuerySystemInformation, 48 bytes per core, ~0.08 ms on 16 cpus.  Take
    two of these and the delta is each core's busy share — the only honest way
    to say "our lane was starved" instead of "our lane was slow".
    """
    global _core_times_buf, _core_times_n
    try:
        if _core_times_buf is None:
            _core_times_n = max(64, (os.cpu_count() or 8) * 2)
            _core_times_buf = (ctypes.c_ubyte * (48 * _core_times_n))()
        used = ctypes.c_ulong(0)
        st = ntdll.NtQuerySystemInformation(
            _SystemProcessorPerformanceInformation, _core_times_buf,
            ctypes.sizeof(_core_times_buf), ctypes.byref(used))
        if st != 0:
            return None
        count = used.value // 48
        raw = bytes(_core_times_buf)
        return [struct.unpack_from("<3Q", raw, i * 48) for i in range(count)]
    except Exception:
        return None


THREAD_QUERY_LIMITED_INFORMATION = 0x0800
k32.GetThreadTimes.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME)]
k32.GetThreadTimes.restype = wintypes.BOOL


def thread_ids(pid: int) -> list[int]:
    """Every thread id of one process.

    A Toolhelp thread snapshot walks every thread on the MACHINE (~36 ms here)
    before the pid filter can run, so callers cache this: an engine's pool keeps
    its thread count for the life of the process.
    """
    out = []
    try:
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        try:
            ent = _ThreadEntry32()
            ent.dwSize = ctypes.sizeof(_ThreadEntry32)
            ok = k32.Thread32First(snap, ctypes.byref(ent))
            while ok:
                if ent.th32OwnerProcessID == pid:
                    out.append(int(ent.th32ThreadID))
                ok = k32.Thread32Next(snap, ctypes.byref(ent))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return out


def thread_cpu_seconds(pid: int, tids=None) -> dict[int, float]:
    """Kernel+user seconds per thread, ~0.02 ms per CACHED thread id.

    Cache the ids (`thread_ids`): the open/read/close of each thread is cheap,
    finding them is not.  An engine's lane threads live as long as the process,
    so one snapshot is enough for a whole session.
    """
    out = {}
    if not pid:
        return out
    for tid in (tids if tids is not None else thread_ids(pid)):
        h = k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, int(tid))
        if not h:
            continue
        try:
            creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if k32.GetThreadTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                  ctypes.byref(kernel), ctypes.byref(user)):
                out[int(tid)] = (_filetime_ticks(kernel)
                                 + _filetime_ticks(user)) / 1e7
        except Exception:
            pass
        finally:
            k32.CloseHandle(h)
    return out


def process_affinity_cpus(pid: int) -> list[int] | None:
    """Logical CPUs a process's HARD affinity mask currently allows.

    The older, coarse mechanism: a 64-bit, group-scoped mask that `pin`
    writes.  Read back so the detailed log can prove what is in force — an
    engine pinned to the pool minus the client's home reports 15 or fewer.
    None when unreadable.
    """
    if not pid:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        proc_mask = ctypes.c_size_t(0)
        sys_mask = ctypes.c_size_t(0)
        ok = k32.GetProcessAffinityMask(h, ctypes.byref(proc_mask),
                                        ctypes.byref(sys_mask))
        if not ok:
            return None
        mask = int(proc_mask.value)
        return [i for i in range(64) if (mask >> i) & 1]
    finally:
        k32.CloseHandle(h)


def process_cpu_set_ids(pid: int) -> list[int] | None:
    """The CPU Set ids this process's threads inherit (its soft preference).

    This is what `apply_core_type` writes: a P/E or per-slot preference the
    engine's own worker threads pick up as they are born.  An empty list is
    "no preference" (Windows decides); None is unreadable.  Read back so the
    detailed log can tell a plan that landed from one that did not.
    """
    if not pid:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        n = wintypes.DWORD(0)
        k32.GetProcessDefaultCpuSets(h, None, 0, ctypes.byref(n))
        count = int(n.value)
        if count <= 0:
            return []
        arr = (wintypes.DWORD * count)()
        if not k32.GetProcessDefaultCpuSets(h, arr, count, ctypes.byref(n)):
            return None
        return [int(x) for x in arr[:int(n.value)]]
    finally:
        k32.CloseHandle(h)


def _cpu_of(entry: dict) -> int:
    return entry["cpu"]


def topology() -> list[dict]:
    """One entry per LOGICAL cpu, in Windows' own order.

    `efficiency` is Windows' `EfficiencyClass`: a HIGHER value is the faster,
    less power-efficient core — so on an Intel hybrid a P-core's class is
    higher than an E-core's (Alder/Raptor Lake report P=1, E=0). That is
    Microsoft's documented meaning, and `hybrid()` above reads it the same way;
    an earlier note here had it backwards. The absolute numbers are not a
    contract — group by comparing the values, never by testing for a literal
    1 or 0.
    """
    n = wintypes.DWORD(0)
    k32.GetLogicalProcessorInformationEx(RelationProcessorCore, None, ctypes.byref(n))
    buf = ctypes.create_string_buffer(n.value)
    if not k32.GetLogicalProcessorInformationEx(RelationProcessorCore, buf, ctypes.byref(n)):
        return []
    raw, out, off = buf.raw[:n.value], [], 0
    while off < len(raw):
        rel, size = struct.unpack_from("<II", raw, off)
        if size == 0:
            break
        if rel == RelationProcessorCore:
            eff = raw[off + 9]
            mask = struct.unpack_from("<Q", raw, off + 32)[0]
            cpus = [i for i in range(64) if mask >> i & 1]
            for c in cpus:
                out.append({"cpu": c, "efficiency": eff, "siblings": cpus})
        off += size
    return sorted(out, key=_cpu_of)


def distribute_threads(total: int, slots: int) -> list[int]:
    """Split logical CPUs across compute slots without hard-coding 8/8/8.

    The remainder goes to the later slots, matching the user's 8-core/16-thread
    example: 16 / 3 -> 5, 5, 6.  When fewer CPUs than slots exist, every slot
    still gets at least one lane.
    """
    slots = max(1, int(slots))
    total = max(slots, int(total))
    base, rem = divmod(total, slots)
    plan = [base] * slots
    for i in range(rem):
        plan[-1 - i] += 1
    return plan


def name(cpu: int) -> str:
    """How the system names it: Task Manager's 'CPU n'."""
    return f"CPU {cpu}"


def mask_of(cpus) -> int:
    m = 0
    for c in cpus:
        m |= 1 << int(c)
    return m


def all_cpus() -> list[int]:
    """Every logical processor this process may use (its own mask)."""
    return [t["cpu"] for t in topology()]


def multi_group() -> bool:
    """True when the machine's CPUs span more than one processor group.

    Windows hands a process a 64-bit affinity mask that is scoped to ONE
    processor group, so a machine with more than 64 logical processors (or
    several groups for any reason) cannot be pinned with `pin()` across the
    boundary.  The CPU Sets API (`cpu_sets`, `set_process_cpu_sets`) is
    group-aware and is used instead; this predicate says when that has to be
    the mechanism rather than the mask.
    """
    try:
        return len({int(c.get("group", 0)) for c in cpu_sets()}) > 1
    except Exception:
        return False


def pin(pid: int, cpus) -> bool:
    """Restrict a running process to `cpus`.

    An EMPTY selection means "no restriction" and is applied as such: it used
    to return False without touching anything, so once a pin had been set the
    only way back to all cores was a restart.

    A MULTI-GROUP machine cannot be pinned here: the mask is 64-bit and one
    group wide, so the CPUs of a second group would be silently dropped (or,
    worse, hit bit 64 and be masked off entirely).  `SetProcessAffinityMask`
    is skipped in that case and the caller's CPU Sets — which ARE group-aware
    and are applied on the same paths — stand instead.  True is returned so a
    caller does not read the skip as a failure.
    """
    cpus = [int(c) for c in (cpus or [])] or all_cpus()
    if not cpus:
        return False
    if multi_group():
        return True
    h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    try:
        return bool(k32.SetProcessAffinityMask(h, mask_of(cpus)))
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------- hybrid CPUs
# `SetThreadSelectedCpuSets` is what Microsoft provides for steering work onto
# P-cores or E-cores; an affinity mask is process-wide and predates hybrid
# parts.  simfleet uses the same pair, and the same rule for telling them
# apart: the HIGHEST EfficiencyClass is the performance core.

class _PowerThrottle(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD),
                ("ControlMask", wintypes.DWORD),
                ("StateMask", wintypes.DWORD)]


PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
ProcessPowerThrottling = 4
TH32CS_SNAPTHREAD = 0x4
THREAD_SET_INFORMATION = 0x0020
THREAD_QUERY_INFORMATION = 0x0040
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


def cpu_model() -> str:
    """The CPU name Windows reports, e.g. "Intel(R) Core(TM) Ultra 9 275HX"."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return ""


def vendor() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "VendorIdentifier")[0])
    except Exception:
        return ""


def cpu_sets() -> list[dict]:
    """One entry per logical processor, as Windows describes it to CPU Sets."""
    size = wintypes.DWORD(0)
    k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(size), None, 0)
    if not size.value:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if not k32.GetSystemCpuSetInformation(buf, size.value, ctypes.byref(size),
                                          None, 0):
        return []
    raw, out, off = buf.raw[:size.value], [], 0
    while off + 20 <= len(raw):
        # SYSTEM_CPU_SET_INFORMATION puts Size FIRST and Type second — the
        # other way round from LOGICAL_PROCESSOR_RELATIONSHIP above.
        rec, typ = struct.unpack_from("<II", raw, off)
        if rec == 0:
            break
        if typ == 0:                      # Type 0 = CpuSetInformation
            cid, group, idx, core, llc, numa, eff, _flags =                 struct.unpack_from("<IHBBBBBB", raw, off + 8)
            out.append({"id": cid, "group": group, "index": idx,
                        "core": core, "llc": llc, "numa": numa,
                        "class": eff})
        off += rec
    return out


def hybrid() -> dict | None:
    """{'p': [...], 'e': [...], 'classes': [...]} on an Intel hybrid, else None.

    P is the HIGHEST EfficiencyClass — the rule simfleet uses, and the one that
    matches this machine: 24 logical processors come back as eight in class 1
    (the eight P-cores) and sixteen in class 0 (the E-cores).
    """
    if vendor() != "GenuineIntel":
        return None
    cs = cpu_sets()
    classes = sorted({c["class"] for c in cs})
    if len(classes) < 2:
        return None
    top = classes[-1]
    return {"p": [c for c in cs if c["class"] == top],
            "e": [c for c in cs if c["class"] != top],
            "classes": classes}


CORE_TYPES = ("auto", "p", "max", "e")


def core_type_label(ct: str) -> str:
    return {"auto": "交给 Windows", "p": "优先 P 核",
            "max": "最高效率优先", "e": "优先 E 核"}.get(
                str(ct or "auto").lower(), str(ct or "auto"))


def ccd_map() -> dict[int, int]:
    """Logical processor index -> its LLC / CCD group, from CPU Sets."""
    return {int(c["index"]): int(c.get("llc", 0)) for c in cpu_sets()}


def _core_units(cs: list[dict]) -> list[list[dict]]:
    """Group CPU Set entries by physical core, in core order."""
    units: dict[tuple, list] = {}
    for c in cs:
        units.setdefault((int(c["group"]), int(c["core"])), []).append(c)
    return [units[k] for k in sorted(units)]


def smt_info(sets: list[dict] | None = None) -> dict:
    """Whether this machine runs more than one thread per physical core.

    x86 SMT is two threads per core; other architectures may report more, so
    the count is read from the CPU Sets rather than assumed to be 2.
    """
    cs = cpu_sets() if sets is None else list(sets)
    units = _core_units(cs)
    per = [len(u) for u in units]
    threads = max(per) if per else 1
    return {"enabled": threads > 1,
            "threads_per_core": threads,
            "physical_cores": len(units),
            "logical_threads": len(cs)}


def group_shape(ids, sets: list[dict] | None = None) -> tuple[int, int]:
    """(physical cores, logical threads) covered by a CPU Set id list."""
    cs = cpu_sets() if sets is None else list(sets)
    want = {int(i) for i in (ids or [])}
    chosen = [c for c in cs if int(c["id"]) in want]
    return len(_core_units(chosen)), len(chosen)


def schedule_groups(slots: int, selected=None,
                    sets: list[dict] | None = None) -> tuple[list[list[int]], str]:
    """CPU Set ids per slot, grouped by LLC (CCD) and then by P/E class.

    A multi-CCD part has one L3/LLC per CCD, so a slot pinned inside one group
    never makes its threads cross the die boundary.  On an Intel hybrid the LLC
    is shared and the efficiency classes stand in for the groups, P first.  A
    slot's slice is contiguous inside its group, so sibling threads stay
    together and no slot spans two LLCs.
    """
    slots = max(1, int(slots))
    cs = _selected_sets(cpu_sets() if sets is None else list(sets), selected)
    if not cs:
        return [], ""
    ordered = _llc_class_groups(cs)
    if len(ordered) >= slots:
        plan = [[int(c["id"]) for c in ordered[i]] for i in range(slots)]
        hint = (f"（建议 slots={len(ordered)} 以用满全部 CCD）"
                if len(ordered) > slots else "")
        return plan, f"{len(ordered)} 组（LLC/类别），{slots} 槽各占一组{hint}"

    # Fewer groups than slots: split each group across the slots it serves.
    base, rem = divmod(slots, len(ordered))
    plan: list[list[int]] = [[] for _ in range(slots)]
    slot = 0
    split_core = False
    for gi, g in enumerate(ordered):
        n = base + (1 if gi < rem else 0)
        parts = _split_group_by_cores(g, n)
        if parts is None:
            # More slots than physical cores: a core's threads must be split.
            parts = _split_group_by_threads(g, n)
            split_core = True
        for part in parts:
            plan[slot] = part
            slot += 1
    note = f"{len(ordered)} 组（LLC/类别），{slots} 槽按整核心分配"
    if split_core:
        note += "（槽位多于核心，部分核心线程被拆分）"
    return plan, note


def _llc_class_groups(cs: list[dict]) -> list[list[dict]]:
    """CPU Sets grouped by (LLC, class), fastest group first."""
    groups: dict[tuple, list] = {}
    for c in cs:
        key = (int(c.get("llc", 0)), int(c.get("class", 0)))
        groups.setdefault(key, []).append(c)
    ordered = sorted(groups.values(),
                     key=lambda g: (-max(int(x["class"]) for x in g),
                                    min(int(x.get("llc", 0)) for x in g)))
    for g in ordered:
        g.sort(key=lambda x: (int(x["core"]), int(x["id"])))
    return ordered


def _split_group_by_cores(g: list[dict], n: int) -> list[list[int]] | None:
    """Split a group into `n` slots by whole physical cores, or None."""
    cores = _core_units(g)
    if len(cores) < n:
        return None
    base, rem = divmod(len(cores), n)
    out: list[list[int]] = []
    idx = 0
    for k in range(n):
        take = base + (1 if k < rem else 0)
        ids: list[int] = []
        for unit in cores[idx:idx + take]:
            ids.extend(int(c["id"]) for c in unit)
        out.append(ids)
        idx += take
    return out


def _split_group_by_threads(g: list[dict], n: int) -> list[list[int]]:
    """Split a group into `n` slots by logical threads (last resort here)."""
    out: list[list[int]] = []
    for k in range(n):
        lo = (len(g) * k) // n
        hi = (len(g) * (k + 1)) // n
        out.append([int(c["id"]) for c in g[lo:hi]])
    return out


def _selected_sets(cs: list[dict], selected) -> list[dict]:
    """The CPU Set records the user's manual pool allows."""
    if not selected:
        return cs
    allow = {int(x) for x in selected}
    return [c for c in cs if int(c["id"]) in allow or int(c["index"]) in allow]


def _unit_meta(unit: list[dict]) -> dict:
    """One physical core, in the terms the balancer decides on."""
    classes = [int(c.get("class", 0)) for c in unit]
    return {"ids": [int(c["id"]) for c in unit],
            "lanes": len(unit),
            "class": max(classes),
            "llc": min(int(c.get("llc", 0)) for c in unit),
            "group": min(int(c.get("group", 0)) for c in unit),
            "core": min(int(c.get("core", 0)) for c in unit)}


def _assign_balanced(units: list[dict], plan: list[list[int]],
                     totals: list[int], counts: list[int]) -> None:
    """Give each whole core to the slot that is furthest behind.

    One core's sibling threads stay together, so two engines never share a
    core's L1/L2.  `totals` is LANES (logical processors) and is what makes
    the slots come out even when P and E cores have different thread counts.
    """
    for u in units:
        slot = min(range(len(plan)),
                   key=lambda k: (totals[k], counts[k], k))
        plan[slot].extend(u["ids"])
        totals[slot] += u["lanes"]
        counts[slot] += 1


def _best_set_id(ids: list[int], sets: list[dict]) -> int | None:
    """The fastest CPU Set id in a slot: highest class, then lowest index."""
    by_id = {int(c["id"]): c for c in sets}
    best = None
    for i in ids:
        c = by_id.get(int(i))
        if c is None:
            continue
        key = (-int(c.get("class", 0)), int(c.get("index", 0)))
        if best is None or key < best[0]:
            best = (key, int(i))
    return best[1] if best else None


def best_set_id(ids, sets: list[dict] | None = None) -> int | None:
    """Public form of `_best_set_id`, for a plan a fallback produced."""
    cs = cpu_sets() if sets is None else list(sets)
    return _best_set_id([int(i) for i in (ids or [])], cs)


def _threads_per_core(sets: list[dict]) -> int:
    units = _core_units(sets)
    return max((len(u) for u in units), default=1)


def _ids_are_known_once(plan: list[list[int]], by_id: dict) -> bool:
    """No repeated CPU and no CPU the system did not describe."""
    seen: set[int] = set()
    for ids in plan:
        for i in ids:
            if int(i) in seen or int(i) not in by_id:
                return False
            seen.add(int(i))
    return True


def _lanes_are_close(plan: list[list[int]], sets: list[dict]) -> bool:
    """The slots' lane counts differ by no more than one core's threads."""
    lanes = [len(g) for g in plan]
    return max(lanes) - min(lanes) <= _threads_per_core(sets)


def _every_slot_has_top(plan: list[list[int]], sets: list[dict],
                        top: int, slots: int) -> bool:
    """Every slot owns a top-tier core, when enough of them exist."""
    top_ids = {int(c["id"]) for c in sets if int(c.get("class", 0)) == top}
    if len(top_ids) < slots:
        return True
    return all(any(int(i) in top_ids for i in ids) for ids in plan)


def _balanced_ok(plan: list[list[int]], primary: list, sets: list[dict],
                 slots: int, top: int) -> bool:
    """Is this plan safe to hand to the engines?

    A plan that leaves a slot empty, repeats a CPU, names a CPU the system did
    not describe, gives a slot no top-tier core while top-tier cores are
    available, or spreads the lanes further than one core's threads apart is
    the blow-up case: the caller must fall back to a plain even split.
    """
    if len(plan) != slots or not all(plan):
        return False
    if len(primary) != slots or any(p is None for p in primary):
        return False
    by_id = {int(c["id"]): c for c in sets}
    return (_ids_are_known_once(plan, by_id)
            and _lanes_are_close(plan, sets)
            and _every_slot_has_top(plan, sets, top, slots))


def _order_ids(ids: list[int], sets: list[dict]) -> list[int]:
    """Fastest core first, so Windows' CPU Set preference starts at the top."""
    by_id = {int(c["id"]): c for c in sets}
    def key(i: int):
        c = by_id.get(int(i), {})
        return (-int(c.get("class", 0)), int(c.get("llc", 0)),
                int(c.get("core", 0)), int(i))
    return sorted((int(i) for i in ids), key=key)


def _unit_lists(units: list[dict], top: int) -> tuple[list[dict], list[dict]]:
    """Top-tier cores and everything else, fastest-first inside each list."""
    key = lambda u: (-u["class"], u["llc"], u["core"])
    p_units = sorted((u for u in units if u["class"] == top), key=key)
    e_units = sorted((u for u in units if u["class"] != top), key=key)
    return p_units, e_units


def schedule_balanced(slots: int, selected=None,
                      sets: list[dict] | None = None
                      ) -> tuple[list[list[int]], list[int | None], str]:
    """Whole physical cores per slot, P and E spread as evenly as the lanes.

    The old grouping handed one whole P/E class or CCD to a slot, so with
    8 P + 16 E and three slots the third engine owned every E core.  Here P
    cores go out first, each to the slot with the fewest lanes; E cores then
    fill the slot with the fewest lanes overall.  Every slot that can have a
    P core gets one, and each slot's CPU Set list is ordered fastest-first so
    a single-threaded task prefers the best core in its own set.
    """
    slots = max(1, int(slots))
    cs = _selected_sets(cpu_sets() if sets is None else list(sets), selected)
    if not cs:
        return [], [], ""
    units = [_unit_meta(u) for u in _core_units(cs)]
    if not units:
        return [], [], ""
    top = max(u["class"] for u in units)
    p_units, e_units = _unit_lists(units, top)
    plan: list[list[int]] = [[] for _ in range(slots)]
    totals = [0] * slots
    _assign_balanced(p_units, plan, totals, [0] * slots)
    _assign_balanced(e_units, plan, totals, [0] * slots)
    ordered = [_order_ids(ids, cs) for ids in plan]
    primary = [_best_set_id(ids, cs) for ids in ordered]
    if not _balanced_ok(ordered, primary, cs, slots, top):
        # The caller's fallback ladder takes it from here.
        return [], [], ""
    return ordered, primary, _balanced_note(p_units, e_units, slots, ordered, cs)


def _balanced_note(p_units, e_units, slots, ordered, cs) -> str:
    """One line saying how the P and E cores landed, for the log and the page."""
    shapes = " / ".join(f"{p}P+{e}E" for p, e in
                        (plan_pk(ids, cs) for ids in ordered))
    return (f"{len(p_units)} 个性能核 + {len(e_units)} 个效率核，"
            f"{slots} 槽按整核心均衡分配：{shapes}")


def schedule_even(slots: int, selected=None,
                  sets: list[dict] | None = None
                  ) -> tuple[list[list[int]], str]:
    """EVERY physical core split as evenly as possible — the last resort.

    Used when the P/E grouping cannot be trusted (a slot with no performance
    core, unknown CPU records, a lane spread wider than one core's threads).

    WHOLE CORES, NOT SINGLE THREADS.  This used to round-robin logical
    processors (`plan[n % slots]`), which puts one core's two SMT siblings in
    DIFFERENT slots — two engines then share a core's L1/L2, and a slot's
    single-lane work (the order/fold/score tail, and every one-lane riven)
    runs on half a physical core while a neighbour's thread fights it for the
    other half.  Handing each whole core to the slot that is furthest behind
    (the same rule `_assign_balanced` uses) keeps the siblings together and
    never splits a core, so the lane counts are still within one core's
    threads of each other and a serial tail gets an entire core to itself.
    """
    slots = max(1, int(slots))
    cs = _selected_sets(cpu_sets() if sets is None else list(sets), selected)
    if not cs:
        return [], ""
    units = [_unit_meta(u) for u in _core_units(cs)]
    # Fastest cores first, so the top-tier cores land in different slots.
    units.sort(key=lambda u: (-u["class"], u["llc"], u["group"], u["core"]))
    plan: list[list[int]] = [[] for _ in range(slots)]
    lanes = [0] * slots
    for u in units:
        slot = min(range(slots), key=lambda k: (lanes[k], k))
        plan[slot].extend(u["ids"])
        lanes[slot] += u["lanes"]
    return plan, f"回退：{len(units)} 个物理核按整核平均分"


def plan_pk(ids, sets: list[dict] | None = None) -> tuple[int, int]:
    """(performance lanes, efficiency lanes) in one slot's CPU Set list."""
    cs = cpu_sets() if sets is None else list(sets)
    by_id = {int(c["id"]): int(c.get("class", 0)) for c in cs}
    vals = [by_id[int(i)] for i in (ids or []) if int(i) in by_id]
    if not vals:
        return 0, 0
    top = max(vals)
    p = sum(1 for v in vals if v == top)
    return p, len(vals) - p


def logical_of_set_ids(ids, sets: list[dict] | None = None) -> list[int]:
    """Logical processor numbers (Task Manager's `CPU n`) for CPU Set ids."""
    cs = cpu_sets() if sets is None else list(sets)
    by_id = {int(c["id"]): int(c["index"]) for c in cs}
    return [by_id[int(i)] for i in (ids or []) if int(i) in by_id]


def core_siblings(logical, sets: list[dict] | None = None) -> list[int]:
    """Logical CPUs of the SAME physical core as `logical`, itself included.

    A single-lane call has nothing to spread across cores, so the fastest thing
    to do with it is give it a WHOLE physical core: the SMT siblings of the
    core it lands on are the hardware threads that core offers, and letting the
    serial work use both (instead of one, while a neighbour thread uses the
    other) is what turns half a core back into a whole one.  Takes and returns
    LOGICAL processor numbers, the units an affinity mask is expressed in.
    Returns `[logical]` when the core is unknown or has no siblings.
    """
    cs = cpu_sets() if sets is None else list(sets)
    me = next((c for c in cs if int(c.get("index", -1)) == int(logical)), None)
    if me is None:
        return [int(logical)]
    kin = [c for c in cs if int(c.get("group", 0)) == int(me.get("group", 0))
            and int(c.get("core", 0)) == int(me.get("core", 0))]
    return sorted(int(c["index"]) for c in kin) or [int(logical)]


def cpu_set_ids(core_type: str) -> tuple[list[int], str]:
    """CPU Set ids for a core-type choice, and a line saying what it means.

    `auto` is a real choice, not a fallback: it clears the sets and lets the
    scheduler decide, which is also what happens on anything that is not an
    Intel hybrid.
    """
    ct = str(core_type or "auto").lower()
    if ct not in ("p", "e"):
        return [], ("由 CCD 分组处理" if ct == "max"
                    else "不绑定，Windows 自行调度")
    h = hybrid()
    if not h:
        return [], "不是 Intel 大小核，不调整"
    return [c["id"] for c in h[ct]], f"{len(h[ct])} 个逻辑处理器"


def thread_ids(pid: int) -> list[int]:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    try:
        ent = _ThreadEntry32()
        ent.dwSize = ctypes.sizeof(_ThreadEntry32)
        out: list[int] = []
        if not k32.Thread32First(snap, ctypes.byref(ent)):
            return []
        while True:
            if ent.th32OwnerProcessID == pid:
                out.append(int(ent.th32ThreadID))
            if not k32.Thread32Next(snap, ctypes.byref(ent)):
                break
        return out
    finally:
        k32.CloseHandle(snap)


def _thread_cpu_sets(pid: int, arr, count: int) -> None:
    for tid in thread_ids(pid):
        th = k32.OpenThread(THREAD_SET_INFORMATION, False, tid)
        if not th:
            continue
        try:
            k32.SetThreadSelectedCpuSets(th, arr, count)
        finally:
            k32.CloseHandle(th)


def set_process_cpu_sets(pid: int, ids) -> bool:
    """Give a process those CPU Sets, and every thread it already has.

    `SetProcessDefaultCpuSets` is the part that matters for a thread pool we do
    not control: threads created LATER inherit it.  The per-thread call covers
    the threads that are already running.
    """
    ids = [int(i) for i in (ids or [])]
    h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    try:
        if not ids:                       # clear back to "no preference"
            ok = bool(k32.SetProcessDefaultCpuSets(h, None, 0))
            _thread_cpu_sets(pid, None, 0)
            return ok
        arr = (wintypes.DWORD * len(ids))(*ids)
        ok = bool(k32.SetProcessDefaultCpuSets(h, arr, len(ids)))
        _thread_cpu_sets(pid, arr, len(ids))
        return ok
    finally:
        k32.CloseHandle(h)


def set_power_throttling(pid: int, slow: bool) -> bool:
    """EcoQoS: ask Windows to treat this process as a background one (or not)."""
    h = k32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    if not h:
        return False
    try:
        st = _PowerThrottle(PROCESS_POWER_THROTTLING_CURRENT_VERSION,
                            PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                            PROCESS_POWER_THROTTLING_EXECUTION_SPEED if slow else 0)
        return bool(k32.SetProcessInformation(
            h, ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)))
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------- process priority
# AFFINITY says WHERE a thread may run; priority says WHO WINS when two
# threads both can.  The client's loop, its TLS handshakes and its logger
# must never queue behind engine work, so the client runs one class above
# normal and the engines one below: an engine only gets CPU the client is
# not using, which is exactly the share a background volunteer wants.

def set_priority_class(pid: int, cls: int) -> bool:
    """Set one process's priority class. False on any API failure."""
    h = k32.OpenProcess(PROCESS_SET_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        return bool(k32.SetPriorityClass(h, cls))
    finally:
        k32.CloseHandle(h)


def priority_class_of(pid: int) -> int | None:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        got = k32.GetPriorityClass(h)
        return int(got) if got else None
    finally:
        k32.CloseHandle(h)


def set_self_priority(cls: int) -> bool:
    """Raise or lower THIS process (the pseudo-handle needs no OpenProcess)."""
    try:
        return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), cls))
    except Exception:
        return False


def priority_label(cls) -> str:
    return {IDLE_PRIORITY_CLASS: "空闲",
            BELOW_NORMAL_PRIORITY_CLASS: "低于标准",
            0x00000020: "标准",
            ABOVE_NORMAL_PRIORITY_CLASS: "高于标准",
            0x00000080: "高",
            0x00000100: "实时"}.get(int(cls or 0), str(cls))


# ---------------------------------------------------- the client's own CPU home
# THE LOG TASK NEEDS A LOGICAL PROCESSOR IT CAN COUNT ON.  Sixteen compute
# threads spread over all sixteen logical processors leave the scheduler
# nothing to hand Python when its loop wakes up: the TLS handshake, the JSON
# fold and the log write all queue behind engine work, a renewal lands late,
# and the server takes the row back.  So the client keeps one thread for
# itself, by this policy:
#
#   * It takes the LEAST performant core on the machine — an E-core on an
#     Intel hybrid, the last core on a flat one.  Logging is light work; the
#     fast cores belong to the fight.
#   * SMT ON: it takes ONE thread of that core.  The sibling stays in the
#     compute pool, so an idle client leaves the whole physical core to the
#     engines (a halted sibling frees its execution units to the other one),
#     and a busy client still owns a thread no engine can take.
#   * SMT OFF (an E-core, or SMT disabled): it takes the whole core.
#   * The compute pool is EVERY other logical processor — never this one.
#
# `log_cpu` in config: "auto" (this policy), "off", or an explicit logical
# processor number for a machine the policy reads wrong.  The pin is CPU Sets
# (`SetThreadSelectedCpuSets`), which is a hard restriction for this process's
# threads, with the affinity mask as the fallback when CPU Sets are absent.

LOG_CPU_AUTO = "auto"
LOG_CPU_OFF = "off"
_log_cpu_cache: tuple | None = None


def physical_units(sets: list[dict] | None = None) -> list[dict]:
    """One entry per PHYSICAL core: ids, lanes, class, llc, group, core."""
    cs = cpu_sets() if sets is None else list(sets)
    return [_unit_meta(u) for u in _core_units(cs)]


def log_cpu_mode(cfg) -> str:
    raw = (cfg or {}).get("log_cpu", LOG_CPU_AUTO)
    return str(raw if raw is not None else LOG_CPU_AUTO).strip().lower()


def _pick_log_core(units: list[dict]) -> dict:
    """The least performant core: lowest class, then the last one of that class.

    Class polarity follows `hybrid()`: the HIGHEST EfficiencyClass is the
    performance core, so the lowest class is the E-core tier.  On a flat
    machine every core shares one class and the tie-break simply takes the
    last core, which keeps the low-numbered cores contiguous for compute.
    """
    ordered = sorted(units, key=lambda u: (int(u["class"]), int(u["llc"]),
                                           int(u["group"]), int(u["core"])))
    lowest = int(ordered[0]["class"])
    tail = [u for u in ordered if int(u["class"]) == lowest]
    return tail[-1]


def _build_log_cpu_plan(cfg) -> dict | None:
    mode = log_cpu_mode(cfg)
    if mode in (LOG_CPU_OFF, "none", "false", "no", "0"):
        return None
    cs = cpu_sets()
    if len(cs) < 2:
        return None                       # one CPU: there is nothing to share
    units = physical_units(cs)
    if not units:
        return None
    if mode in (LOG_CPU_AUTO, ""):
        unit = _pick_log_core(units)
    else:
        try:
            want = int(mode)
        except ValueError:
            return None                   # a typo is "no pin", never a crash
        by_id = {int(c["id"]): c for c in cs}
        unit = None
        for u in units:
            logicals = [int(by_id[i]["index"]) for i in u["ids"]]
            if want in u["ids"] or want in logicals:
                unit = u
                break
        if unit is None:
            return None
    ids = sorted(int(i) for i in unit["ids"])
    # ONE thread of the core — the higher CPU Set id, the second SMT sibling.
    # The primary (lower id) sibling stays with the compute pool.
    home = ids[-1]
    compute = [int(c["id"]) for c in cs if int(c["id"]) != home]
    by_id = {int(c["id"]): c for c in cs}
    entry = by_id[home]
    classes = sorted({int(c.get("class", 0)) for c in cs})
    top = classes[-1] if classes else 0
    kind = ("E 核" if len(classes) > 1 and int(entry.get("class", 0)) != top
            else "P 核" if len(classes) > 1 else "核心")
    ht = len(ids) > 1
    siblings = [i for i in ids if i != home]
    if ht:
        sib_logical = [int(by_id[i]["index"]) for i in siblings]
        why = (f"{kind}（类别 {entry.get('class')}）SMT 开：本进程占逻辑线程 "
               f"{int(entry['index'])}，同物理核心的另一线程 "
               f"{'/'.join(str(n) for n in sib_logical)} 留给计算进程")
    else:
        why = (f"{kind}（类别 {entry.get('class')}）无超线程：整核 "
               f"{int(entry['index'])} 归本进程，计算进程用其余全部逻辑处理器")
    return {"set_id": home, "logical": int(entry["index"]),
            "group": int(entry.get("group", 0)), "core": int(entry.get("core", 0)),
            "class": int(entry.get("class", 0)), "ht": ht,
            "siblings": siblings, "compute_ids": compute, "reason": why}


def log_cpu_plan(cfg=None, refresh: bool = False) -> dict | None:
    """The client's own logical processor under the log-task policy.

    None means "no pin": a single-CPU machine, `log_cpu: "off"`, or a choice
    the topology cannot place.  Cached per config value — the topology of a
    running machine does not move.
    """
    global _log_cpu_cache
    key = log_cpu_mode(cfg)
    if not refresh and _log_cpu_cache is not None and _log_cpu_cache[0] == key:
        return _log_cpu_cache[1]
    plan = _build_log_cpu_plan(cfg)
    _log_cpu_cache = (key, plan)
    return plan


def pin_log_cpu(plan: dict) -> bool:
    """Restrict THIS process to the plan's home, threads included."""
    if plan.get("set_id") is not None:
        return set_process_cpu_sets(os.getpid(), [int(plan["set_id"])])
    return pin(os.getpid(), [int(plan["logical"])])


def apply_log_cpu_policy(cfg=None, refresh: bool = False) -> dict | None:
    """Pin the current process to its log-task home. Returns the plan or None."""
    plan = log_cpu_plan(cfg, refresh=refresh)
    if not plan:
        return None
    plan["applied"] = bool(pin_log_cpu(plan))
    return plan


def log_cpu_label(plan) -> str:
    """One line for the console and the core page."""
    if not plan:
        return "未绑定（客户端随系统调度）"
    if plan.get("applied") is False:
        return f"第 {plan['logical']} 号逻辑处理器（设置失败，未生效）"
    return f"第 {plan['logical']} 号逻辑处理器：{plan['reason']}"


if __name__ == "__main__":
    topo = topology()
    print(f"{len(topo)} logical CPUs")
    for t in topo:
        print(f"  {name(t['cpu']):>7}   efficiency class {t['efficiency']}")
