# -*- coding: utf-8 -*-
"""整包更新路径的回归测试（现场事故 2026-10-17 14:16）。

现场事故的完整因果：整包替换其实**成功**了（替换 1770 个文件、engine/wfsim-core.exe
也换成了新的 5a8b8f55fb10 / sha256 592f6553…），但 `_relaunch()` 里的

    bat = paths.ROOT / "run-portable.bat"

`paths.ROOT` 在 src/paths.py 里是 **str**（`os.path.dirname`），str 除以 str 直接抛
`TypeError: unsupported operand type(s) for /: 'str' and 'str'`。异常冒到
`_auto_update_locked` 的兜底分支 → `_abort_update`/`_rollback_core` →
`_restore_old_state()` 把**更新前**的 state.json 写回去（engine=c461ec36fbcd、
sha256=7d05f937），于是"磁盘上的引擎是新的、账本说是旧的"⇒ 每一次检查都判定"没更新
成功"，反复重下 22.6 MB 整包，每次都在同一行崩掉。

本测试钉住四件事：① 不许再出现 str 除法；② 整包替换到位之后不许回滚账本（整包没有
wfsim-core.prev.exe，src/ 也回不去）；③ 替换后要把包里的 state.json 采纳进来；
④ 顺手修掉的重复日志行/错位文案别再回来。

用法：python test_update_paths.py [包目录]      （默认取 CI 包）
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                    pathlib.Path(__file__).resolve().parent
                    / "client-portable-20261017-0345-ci").resolve()
SRC = ROOT / "src"
APP = (SRC / "app.py").read_text(encoding="utf-8")
FLEET = (SRC / "fleet.py").read_text(encoding="utf-8")
SYNC = (SRC / "sync_engine.py").read_text(encoding="utf-8")
MANIFEST = ((ROOT / "ci" / "make_manifest.py").read_text(encoding="utf-8")
            if (ROOT / "ci" / "make_manifest.py").is_file() else "")
WF = ROOT / ".github" / "workflows"
HAS_CI = WF.is_dir()

checks: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    checks.append((name, bool(ok)))


# --- ① 任何地方都不许把 paths.<常量> 当 pathlib.Path 用 -----------------------
offenders = []
for p in sorted(SRC.glob("*.py")):
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"paths\.[A-Z_]+ *[/]", line):
            offenders.append(f"{p.name}:{i}")
check("src 里没有 paths.<常量> / 的 str 除法", not offenders)
for o in offenders:
    print("     str 除法残留:", o)

# --- ② _relaunch：join + isfile，且都在 try 里 --------------------------------
relaunch = APP[APP.index("async def _relaunch("):]
relaunch = relaunch[:relaunch.index("async def _resume_after_update(")]
check("_relaunch 用 os.path.join(paths.ROOT, 'run-portable.bat')",
      'os.path.join(paths.ROOT, "run-portable.bat")' in relaunch)
check("_relaunch 用 os.path.isfile 判断批处理",
      "os.path.isfile(bat)" in relaunch and "bat.is_file()" not in relaunch)
check("bat 的拼法在 try 之内（异常必须被自己兜住，不能冒到回滚）",
      relaunch.index("os.path.join(paths.ROOT") > relaunch.index("try:"))
check("包目录里 run-portable.bat 真的存在，join 出来的路径能命中",
      os.path.isfile(os.path.join(str(ROOT), "run-portable.bat")))

# --- ③ 整包替换到位后不许回滚账本 ---------------------------------------------
install = APP[APP.index("async def _install_and_restart("):]
install = install[:install.index("async def _relaunch(")]
check("_install_and_restart 在整包分支里置 _package_installed = True",
      "self._package_installed = True" in install)
check("置位发生在 _relaunch() 之前（否则重启失败时兜底又会回滚）",
      0 < install.index("self._package_installed = True")
      < install.index("await self._relaunch()"))
check("替换后采纳包里的 state.json（known_engine / local_engine）",
      "sync_engine.load_state" in install and 'st["engine"]' in install)

locked = APP[APP.index("async def _auto_update_locked("):]
locked = locked[:locked.index("async def _manual_fast_rebuild(")]
check("_auto_update_locked 的 except 里对整包做了区分",
      "if self._package_installed:" in locked)
check("整包分支不调用 _abort_update（回滚会写回旧 state.json）",
      locked.index("if self._package_installed:") < locked.index(
          "await self._abort_update("))
check("整包分支给出人工重启的提示",
      "请手动重开客户端以启用新版本" in locked)
check("finally 的兜底回滚排除整包情形",
      "and not self._package_installed" in locked)
check("引擎线仍然保留回滚（else: _abort_update）",
      "else:" in locked[locked.index("if self._package_installed:"):
                        locked.index("await self._abort_update(") + 40])

# --- ④ 重复日志与错位文案 ------------------------------------------------------
cli = SYNC[SYNC.index('if args.install_portable is not None:'):]
cli = cli[:cli.index("try:", 10)]
check("--install-portable 分支不再重复打印 install_portable 的返回值",
      "ok, _msg = install_portable(" in cli and "\n        log(msg)" not in cli)
check("install_portable 自己仍然打印汇总行",
      re.search(r"\n    log\(msg\)\n    return True, msg", SYNC) is not None)
check("等待空闲的文案按整包/引擎线区分",
      'what = "整包已下载" if self._package_pending else "新核心已就绪"' in install
      and "新核心构建完成；等待" not in install)
check("fleet.py 用 pathlib.Path(paths.ROOT) 拼任务落盘路径",
      'pathlib.Path(paths.ROOT) / "data" / "task_answers.json"' in FLEET)

# --- ⑤ CI（只在仓库包里查，运行包没有 .github） --------------------------------
if HAS_CI:
    be = (WF / "build-engine.yml").read_text(encoding="utf-8")
    rp = (WF / "repack.yml").read_text(encoding="utf-8")
    check("build-engine 的 refresh 用 always() && （否则 need.result=='failure' 恒假）",
          "always() && needs.verify.result == 'failure'" in be)
    check("repack 的锚点冒烟不拦发布",
          re.search(r"name: smoke test the packed engine.*?continue-on-error: true",
                    rp, re.S) is not None)
    wb = ROOT / "ci" / "tools" / "wasm_baseline.mjs"
    if wb.is_file():
        check("wasm_baseline 写出前建目录（dist/ 可能不存在）",
              "mkdirSync(path.dirname(path.resolve(out)), { recursive: true })"
              in wb.read_text(encoding="utf-8"))

# --- ⑤ 客户端清单：固定 engine id 下改了 src/ 也必须能被发现 ---------------
# 现场事实（2026-10-18）：两条修复只动了 src/，engine id 没变，于是只有手工
# 换过文件的副本拿到了它们。清单就是这个缺口的补丁。
check("sync_engine 有 client_manifest_url", "def client_manifest_url" in SYNC)
_url = SYNC.split("def client_manifest_url", 1)[1].split("\ndef ", 1)[0] if "def client_manifest_url" in SYNC else ""
check("清单地址用 update_repo()（不是写死的上游 REPO）",
      "update_repo()" in _url and "magenie33" not in _url)
check("清单资产名 = client-manifest.json",
      'CLIENT_MANIFEST_ASSET = "client-manifest.json"' in SYNC)
check("清单覆盖 src/ 下全部 py（客户端照着清单比，双方不会各留一份表）",
      "for name, sha in sorted(want.items())" in SYNC
      and "CLIENT_MANIFEST_SOURCES" not in SYNC)
check("清单脚本现算 src/*.py，不再手写十个名字",
      "source_names(root)" in MANIFEST
      and '(root / "src").glob("*.py")' in MANIFEST)
check("client_manifest_drift 用 sha256 逐文件比",
      "def client_manifest_drift" in SYNC and "file_sha256(path) != str(sha)" in SYNC)
check("清单取不到时不算落后（None 不是 up to date）",
      "if not isinstance(info, dict):\n        return out" in SYNC)
check("release_asset_sha256 不再拿上游 REPO 拼标签",
      "api.github.com/repos/{REPO}/releases/tags/{tag}" not in SYNC)
check("pending-update 存/读/清三件套",
      all(s in SYNC for s in ("def save_pending_update", "def load_pending_update",
                              "def clear_pending_update")))
check("有 latest 标签直读（不花 API 额度）", "def latest_portable_tag" in SYNC
      and "releases/latest" in SYNC)
check("app 有 _update_mode()", "def _update_mode" in APP)
check("自编译包默认 notify、预编译包默认 auto（判定只有一份）",
      "def update_mode_of(cfg: dict, core_source: str) -> str:" in APP
      and 'return ("notify" if core_source == sync_engine.CORE_SOURCE_LOCAL' in APP)
check("设置页与主程序共用同一份判定", APP.count("update_mode_of(") >= 2)
check("设置页能读回待确认的更新（重启也不丢）",
      "sync_engine.load_pending_update()" in APP)
check("notify 模式只提醒不替换（_note_pending_update）",
      "def _note_pending_update" in APP and "提醒模式，不自动替换" in APP)
check("设置页有更新方式开关与两个按钮",
      all(s in APP for s in ('id="update_mode_toggle"', 'id="update_now"', 'id="update_later"')))
check("config 有 update_mode 键（空 = 跟随包类型）",
      '"update_mode": ""' in (SRC / "config.py").read_text(encoding="utf-8"))
check("ci/make_manifest.py 存在", (ROOT / "ci" / "make_manifest.py").is_file())
if HAS_CI:
    _rp = (WF / "repack.yml").read_text(encoding="utf-8")
    _rl = (WF / "release.yml").read_text(encoding="utf-8")
    for _name, _txt in (("repack", _rp), ("release", _rl)):
        check(f"{_name} 生成客户端清单", "make_manifest" in _txt)
        check(f"{_name} 把清单列进发布资产", "dist/client-manifest.json" in _txt)
    check("repack 发布步用 steps.pick 的 engine",
          '--engine", "${{ steps.pick.outputs.engine }}"' in _rp)
    check("release 发布步用 steps.ident 的 engine",
          '--engine", "${{ steps.ident.outputs.engine }}"' in _rl)


# 功能性：清单漂移判定 + pending-update 存取（不联网，只碰临时目录）
import importlib.util as _iu  # noqa: E402
import json as _json  # noqa: E402
import tempfile as _tmp  # noqa: E402

sys.path.insert(0, str(SRC))
try:
    _spec = _iu.spec_from_file_location("sync_engine_t", str(SRC / "sync_engine.py"))
    _se = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_se)
    with _tmp.TemporaryDirectory() as _d:
        _root = pathlib.Path(_d)
        (_root / "src").mkdir()
        (_root / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
        _se.ROOT = _root
        _se.PENDING_UPDATE = _root / "engine-src" / "pending-update.json"
        _good = _se.file_sha256(_root / "src" / "app.py")
        _drift = _se.client_manifest_drift({"engine": "e", "commit": "c",
                                            "src": {"src/app.py": _good}})
        check("清单一致 -> 不落后", _drift["behind"] is False and _drift["checked"] == 1)
        _drift = _se.client_manifest_drift({"src": {"src/app.py": "0" * 64}})
        check("内容不同 -> 落后且点名文件",
              _drift["behind"] is True and _drift["changed"] == ["src/app.py"])
        _drift = _se.client_manifest_drift({"src": {"src/gone.py": _good}})
        check("本地缺文件 -> 落后且点名文件",
              _drift["behind"] is True and _drift["missing_local"] == ["src/gone.py"])
        check("没有清单 -> 不落后（无信号，不是最新）",
              _se.client_manifest_drift(None)["behind"] is False
              and _se.client_manifest_drift(None)["have_manifest"] is False)
        _se.save_pending_update({"reason": "客户端代码 1 个文件", "engine": "e"})
        check("pending-update 写进去读得出",
              (_se.load_pending_update() or {}).get("reason") == "客户端代码 1 个文件")
        _se.clear_pending_update()
        check("pending-update 清得掉", _se.load_pending_update() is None)
except Exception as _e:                                          # noqa: BLE001
    check(f"清单功能性自检可跑（{type(_e).__name__}: {_e}）", False)

ok_n = sum(1 for _, v in checks if v)
bad_n = len(checks) - ok_n
for name, v in checks:
    print("  %s %s" % ("PASS" if v else "FAIL", name))
print("%d passed, %d failed   （包：%s）" % (ok_n, bad_n, ROOT))
sys.exit(0 if bad_n == 0 and ok_n else 1)
