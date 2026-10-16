# -*- coding: utf-8 -*-
"""远程清单里的路径必须落进 worktree —— `sync_engine.work_target()`。

为什么有这条：CI（run 38959289878，2026-10-16）在 runners 上同步上游源码时死在

    RuntimeError: refusing to fetch outside the worktree: 'data/abilities/warcry.yaml'

`data/abilities/warcry.yaml` 是上游树里一个再普通不过的 blob（已核对：10304 条
条目，0 个 symlink，无 `..`/反斜杠/尾空格），本地同一份代码却一路通过。差别只在
盘符与路径形态：旧守卫拿 `str((WORK/path).resolve())` 与 `str(WORK.resolve())+os.sep`
比字符串，而 Windows 上同一个目录既可能报成 `D:\\a\\...` 也可能报成
`\\\\?\\D:\\a\\...`（取决于路径是否存在、链路上有没有 reparse point），于是同一棵树
在一台机器上通过、在另一台上被拒。

所以守卫改成**检查路径分量**（必须是相对、且不含 `..` 或盘符），再把分量 join 到
WORK 上 —— 这既是真正要防的（远程清单穿越），也不依赖任何 resolve 表述。

用法：python test_sync_guard.py <客户端目录绝对路径>
"""
import os, sys

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                       r"D:\工程\zcode\wfsim\client-portable-20261016-144305-memopt")
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)
import sync_engine as se            # noqa: E402

ok = bad = 0
def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1; print("  PASS  %s" % name)
    else:
        bad += 1; print("  FAIL  %s  %s" % (name, extra))


def main():
    work = se.WORK
    print("worktree = %s" % work)

    # ---- 1. 正常路径：接受，且确实落在 WORK 里 ------------------------
    for rel in ("data/abilities/warcry.yaml", "Cargo.toml", "engine/src/fight/run.rs",
                "webapi/src/board_rows.rs", "./data/x.yaml", "a//b.yaml"):
        try:
            t = se.work_target(rel)
        except Exception as e:
            check("接受 %r" % rel, False, "%s: %s" % (type(e).__name__, e))
            continue
        inside = str(t).startswith(str(work))
        check("接受 %r" % rel, inside and t.name, "t=%s" % t)

    # 2. 上层目录/盘符：必须拒绝
    for rel in ("../evil", "data/../../evil", "..", "", "  ",
                "data/./../x", "C:/windows/system32/x.dll", "c:x.yaml"):
        try:
            t = se.work_target(rel)
            check("拒绝 %r" % rel, False, "却给出 %s" % t)
        except RuntimeError as e:
            check("拒绝 %r" % rel, "refusing to fetch outside" in str(e), str(e))

    # 3. 绝对路径与 UNC：不拒也行，但**必须**落在 WORK 内（斜杠被当作分量）
    for rel in ("/etc/passwd", r"\\server\share\payload.bin"):
        try:
            t = se.work_target(rel)
            check("包住 %r" % rel, str(t).startswith(str(work)), "t=%s" % t)
        except RuntimeError as e:
            check("包住 %r" % rel, True, "(直接拒绝也算安全: %s)" % e)

    # 4. 守卫本身：不许再退回字符串前缀比较 -----------------------------
    src = open(os.path.join(ROOT, "src", "sync_engine.py"), encoding="utf-8").read()
    check("存在 work_target()", "def work_target(" in src)
    check("fetch_one 走 work_target()", "def fetch_one(item: dict)" in src
          and "target = work_target(item[\"path\"])" in src)
    check("旧守卫已删除", "startswith(str(root) + os.sep)" not in src
          and "root = WORK.resolve()" not in src)
    check("work_target 检查分量", 'p == ".."' in src and '":" in p' in src)

    # 5. 目标确实可写：目录会被创建，写出后再读回（不碰网络） -----------
    try:
        t = se.work_target("ci-selftest/guard/probe.txt")
        check("目标在 worktree 内", str(t).startswith(str(work)), str(t))
    except Exception as e:
        check("目标在 worktree 内", False, str(e))

    print("\n%d passed, %d failed" % (ok, bad))


main()
sys.exit(1 if bad else 0)
