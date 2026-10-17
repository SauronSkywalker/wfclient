# -*- coding: utf-8 -*-
"""手动版本上报开关的回归测试（用户要求，2026-10-17）。

语义：
  * 设置页「上报服务器版本」一行右侧多一个开关「手动 开 / 手动 关」，**默认关**，
    打开时是黄底（Textual 的 `.on`）。
  * 关：`config.json` 里的 `wire_engine` **完全不读**，上报版本跟随本机已安装核心。
    这就是“配置文件里写的版本号才会被读取”的闸门；顺手解决现场问题——把旧包的
    `data/` 拷进新包后，标题栏一直显示过去那个版本号。
  * 开：配置里的版本号才生效，± 按钮才可用。
  * 开/关都可能改变上报的 engine id，所以都要先排空（drain）再切。

用法：python test_wire_manual.py <包目录>
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve()
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
import app as A          # noqa: E402
import config as C       # noqa: E402
import fleet as F        # noqa: E402
import sync_engine as SE  # noqa: E402

PASS = 0
FAIL = 0


def ck(name, got, want=True):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r} want {want!r}")


APP = (SRC / "app.py").read_text(encoding="utf-8")
FLEET = (SRC / "fleet.py").read_text(encoding="utf-8")
CFG = (SRC / "config.py").read_text(encoding="utf-8")

print("--- ① 静态：三条链上都有开关 ---")
ck("config DEFAULTS 有 wire_manual 且默认 False",
   C.DEFAULTS.get("wire_manual") is False)
ck("wire_manual 不在 DROPPED_KEYS", "wire_manual" not in C.DROPPED_KEYS)
ck("fleet 声明 wire_manual 属性", "self.wire_manual: bool = False" in FLEET)
ck("fleet 从 cfg 读开关", 'self.wire_manual = bool(self.cfg.get("wire_manual"))' in FLEET)
ck("fleet 只在开关打开时读 wire_engine",
   'if self.wire_manual else ""' in FLEET)
ck("fleet 不再无条件读 wire_engine",
   'configured_wire = str(self.cfg.get("wire_engine") or "").strip()' not in FLEET)
ck("compose 里 ± 右侧有开关按钮",
   'yield Button("手动 关", id="wire_manual_toggle")' in APP)
ck("按钮映射到 _on_wire_manual", '"wire_manual_toggle": "_on_wire_manual"' in APP)
ck("SettingsScreen 收 on_wire_manual 回调", "on_wire_manual=None" in APP)
ck("回调存起来", "self.on_wire_manual = on_wire_manual" in APP)
ck("_refresh 写开关文案",
   'manual_button.label = "手动 开" if manual else "手动 关"' in APP)
ck("_refresh 给开关上黄底 .on",
   'manual_button.set_class(manual, "on")' in APP)
ck("关着时 ± 一律灰掉",
   "if not manual:\n                can_prev = can_next = False" in APP)
ck("开关有固定宽度（放得下“手动 开/关”）", "#wire_manual_toggle { width: 11" in APP)
ck("打开设置页时把回调接上", "on_wire_manual=self._set_wire_manual" in APP)
ck("App 实现 _set_wire_manual", "async def _set_wire_manual" in APP)
shift = APP.split("def _start_engine_shift")[1].split("async def _run_engine_shift")[0]
ck("± 的入口也有开关闸门",
   'if not bool(self.fleet.cfg.get("wire_manual")):' in shift)

print("--- ② 行为：fleet 侧闸门（真方法 _apply_engine_ids） ---")
SE.load_state = lambda: {}          #: 不读盘上的 state.json


class FakeFleet:
    _effective_engine = F.Fleet._effective_engine
    _learn_proto_served = lambda self, rel: None

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.on_event = lambda *a, **k: None


def engine_ids(cfg, rel, state=None):
    SE.load_state = (lambda: dict(state)) if state is not None else (lambda: {})
    f = FakeFleet(cfg)
    F.Fleet._apply_engine_ids(f, dict(rel))
    return f.wire_manual, f.wire_engine, f.engine_id


m, w, e = engine_ids({"known_engine": "bbbb", "wire_engine": "aaaa",
                      "wire_manual": False}, {"engine": "bbbb"})
ck("关：wire_engine 被忽略（跟本机）", (m, w, e), (False, "bbbb", "bbbb"))

m, w, e = engine_ids({"known_engine": "bbbb", "wire_engine": "aaaa",
                      "wire_manual": True}, {"engine": "cccc"})
ck("开：手动版本生效", (m, w, e), (True, "aaaa", "aaaa"))

m, w, e = engine_ids({"known_engine": "bbbb", "wire_engine": "aaaa",
                      "wire_manual": True}, {"engine": "bbbb"})
ck("开：本机已是最新时仍不冒旧 id（join-only 防护）",
   (m, w, e), (True, "aaaa", "bbbb"))

m, w, e = engine_ids({"wire_engine": "aaaa", "wire_manual": False},
                     {"engine": "dddd"}, state={"engine": "eeee"})
ck("关：安装记录优先，配置里的 id 完全不参与",
   (m, w, e), (False, "eeee", "eeee"))

print("--- ③ 行为：App 侧开关（真方法 _set_wire_manual） ---")
_saves: list[dict] = []
_orig_save = A.config.save
A.config.save = lambda cfg: _saves.append(dict(cfg))


class FakePool:
    _effective_engine = F.Fleet._effective_engine

    def __init__(self, local, served, wire, effective):
        self.local_engine = local
        self.served_engine = served
        self.wire_engine = wire
        self.engine_id = effective
        self.wire_manual = False
        self.paused = False
        self.maintenance = False
        self.drained = False

    async def pause_drain(self):
        self.paused = True
        self.drained = True

    async def resume_from_pause(self):
        self.paused = False


class FakeApp:
    _apply_wire_switch = A.WfSimClient._apply_wire_switch
    #: `_follow_wire_value` is a staticmethod on the App, so keep it static here
    #: too: as a plain class attribute it would take `self` and blow up.
    _follow_wire_value = staticmethod(A.WfSimClient._follow_wire_value)
    _set_wire_manual = A.WfSimClient._set_wire_manual

    def __init__(self, pool, cfg):
        self.fleet = pool
        self.cfg = dict(cfg)
        self._updating = False
        self._engine_switch_lock = asyncio.Lock()
        self.events = []

    def _event(self, kind, msg):
        self.events.append((kind, msg))

    async def _wait_update_idle(self, timeout=None):
        return True


def set_manual(pool, cfg, enable):
    app = FakeApp(pool, cfg)
    asyncio.run(app._set_wire_manual(enable))
    return app


pool = FakePool("bbbb", "cccc", "bbbb", "bbbb")
cfg = {"wire_engine": "aaaa", "wire_manual": False}
app = set_manual(pool, cfg, True)
ck("开：cfg 记下开关", app.cfg.get("wire_manual"), True)
ck("开：fleet 记下开关", pool.wire_manual, True)
ck("开：手动版本写进 fleet", pool.wire_engine, "aaaa")
ck("开：上报 id 换成手动版本", pool.engine_id, "aaaa")
ck("开：配置里保留显式旧 id（不是跟随）", app.cfg.get("wire_engine"), "aaaa")
ck("开：先排空再切", pool.drained, True)
ck("开：切完恢复运行", pool.paused, False)
ck("开：有事件说明", any("手动版本上报：开" in m for _, m in app.events), True)

pool = FakePool("bbbb", "cccc", "bbbb", "bbbb")
cfg = {"wire_engine": "", "wire_manual": False}
app = set_manual(pool, cfg, True)
ck("开（还没选过版本）：不切 id", pool.engine_id, "bbbb")
ck("开（还没选过版本）：不排空", pool.drained, False)
ck("开（还没选过版本）：提示用 ± 选", any("+/−" in m or "+/-" in m for _, m in app.events), True)

pool = FakePool("bbbb", "cccc", "aaaa", "aaaa")
cfg = {"wire_engine": "aaaa", "wire_manual": True}
app = set_manual(pool, cfg, False)
ck("关：cfg 开关关闭", app.cfg.get("wire_manual"), False)
ck("关：清掉配置里的版本号", app.cfg.get("wire_engine"), "")
ck("关：切回本机安装的 id", pool.engine_id, "bbbb")
ck("关：先排空再切", pool.drained, True)
ck("关：有事件说明", any("手动版本上报：关" in m for _, m in app.events), True)

pool = FakePool("bbbb", "bbbb", "bbbb", "bbbb")
cfg = {"wire_engine": "", "wire_manual": True}
app = set_manual(pool, cfg, False)
ck("关：id 无需变化时不排空", pool.drained, False)

A.config.save = _orig_save

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
