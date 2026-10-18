"""发货包：ver 归属校验（用户要求 m06136，滚动日志全覆盖 m01103）

启动时、以及 ver 集合每次变更（refresh_verifiers）后都要再校验一遍：
  * 无主的 verifier          -> 立刻登记到当前账号名下；
  * 属于别的账号             -> 换成一个新的无主 id（自动切换）；
  * 属于当前账号 / 归属未知  -> 不动。

并且**每一个 verifier 都要在滚动日志里留下一行**（无论结论是什么），
最后再用一行小结四种结论各多少个，这样「这台机器上的 ver 到底是谁的」
不用看代码、只看日志就能回答。

用法：python test_verifier_owner.py <包目录>
"""
import asyncio
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT / "src"))
import fleet as F  # noqa: E402

PASS = 0
FAIL = 0


def ck(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r} want {want!r}")


def ev(f, name):
    """The fields of the first structured event with this name (or None)."""
    for n, kw in f.log.events:
        if n == name:
            return kw
    return None


def ev_all(f, name):
    return [kw for n, kw in f.log.events if n == name]


def lines(f):
    return [m for _, m in f.events]


class FakeLog:
    def __init__(self):
        self.events = []

    def event(self, name, **kw):
        self.events.append((name, kw))


class FakeRemote:
    """`points`/`claim` are driven by a table; `ledger` decides "whose"."""

    def __init__(self, *, devices=(), points=None, ledger=None, claim_ok=True):
        self.cookie = "session"
        self._devices = list(devices)
        self._points = dict(points or {})
        self._ledger = dict(ledger or {})
        self.claim_ok = claim_ok
        self.claimed_calls = []

    def account_devices(self):
        return {"ok": True, "devices": [{"id": d} for d in self._devices]}

    def points(self, vid, since):
        return dict(self._points.get(vid, {"ok": True, "claimed": False}))

    def board_ledger(self, vid, since):
        return dict(self._ledger.get(vid, {"ok": True, "hours": []}))

    def claim(self, vid, label):
        self.claimed_calls.append((vid, label))
        return {"ok": self.claim_ok}


async def fake_io(fn, *a, **kw):
    return fn(*a, **kw)


def make_fleet(remote, verifiers, *, signed_in=True, claimed=()):
    f = object.__new__(F.Fleet)
    f.remote = remote
    f.cfg = {"verifiers": list(verifiers), "max_verifiers": len(verifiers) or 6,
             "claimed": list(claimed), "active_verifiers": len(verifiers) or 6,
             "base_verifiers": len(verifiers) or 6}
    f.verifiers = [F.Verifier(v, label=f"v{i+1:02d}") for i, v in enumerate(verifiers)]
    f.account_signed_in = signed_in
    f._io = fake_io
    events = []
    f.on_event = lambda kind, msg: events.append((kind, msg))
    f.events = events
    f.on_config_changed = lambda: None
    f.log = FakeLog()
    return f


def test_unowned_is_registered():
    print("无主 -> 登记到当前账号")
    r = FakeRemote(devices=("ours01",), points={"aaaa1111": {"ok": True, "claimed": False}})
    f = make_fleet(r, ["aaaa1111"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("返回登记数量", n, 1)
    ck("claim 被调用", [v for v, _ in r.claimed_calls], ["aaaa1111"])
    ck("标签用 wfsimpp NN", r.claimed_calls[0][1], "wfsimpp 01")
    ck("标记为已认领", f.verifiers[0].account_claimed, True)
    ck("写入 cfg.claimed", f.cfg["claimed"], ["aaaa1111"])
    ck("登记事件带 tasks_held=0", ev(f, "claim_begin")["tasks_held"], 0)
    ck("登记事件带 reason", ev(f, "claim_begin")["reason"], "启动")
    ck("on_event 有提示", any("登记到当前账号" in m for _, m in f.events), True)
    ck("逐个归属事件 state=unowned", ev(f, "verifier_ownership")["state"], "unowned")
    ck("逐个归属行说得清楚", any("无主 → 登记到当前账号" in m for m in lines(f)), True)


def test_foreign_is_switched():
    print("别人的 -> 换成新的无主 id，并登记")
    r = FakeRemote(devices=("ours01",),
                   points={"bbbb2222": {"ok": True, "claimed": True}},
                   ledger={"bbbb2222": {"ok": True,
                                        "hours": [{"hour": "2026-10-17T00", "holder": "other"}]}})
    f = make_fleet(r, ["bbbb2222"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    new = f.cfg["verifiers"][0]
    ck("旧 id 已被替换", new != "bbbb2222", True)
    ck("新 id 已登记", [v for v, _ in r.claimed_calls], [new])
    ck("返回登记数量", n, 1)
    ck("换掉时留言", any("不属于当前账号" in m for _, m in f.events), True)
    ck("逐个归属事件 state=foreign", ev(f, "verifier_ownership")["state"], "foreign")
    ck("逐个归属行点到新 id", any(f"新的无主 id {new[:12]}" in m for m in lines(f)), True)
    ck("逐个归属事件带新 id", ev(f, "verifier_ownership")["replacement"], new)


def test_own_is_left_alone():
    print("自己的（本地已登记）-> 不动")
    r = FakeRemote(devices=("cccc33",),
                   points={"cccc3333": {"ok": True, "claimed": True}},
                   ledger={"cccc3333": {"ok": True,
                                        "hours": [{"hour": "2026-10-17T00", "holder": "you"}]}})
    f = make_fleet(r, ["cccc3333"], claimed=["cccc3333"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("未替换", f.cfg["verifiers"], ["cccc3333"])
    ck("未重复认领", r.claimed_calls, [])
    ck("返回登记数量", n, 0)
    ck("逐个归属行说明是自己的", any("属于当前账号（本地已登记）" in m for m in lines(f)), True)
    ck("逐个归属事件 state=yours", ev(f, "verifier_ownership")["state"], "yours")


def test_own_but_not_recorded_locally():
    print("自己的、但本地没登记 -> 仍然不动，日志点名这个差异")
    r = FakeRemote(devices=("cccc33",),
                   points={"c0c03333": {"ok": True, "claimed": True}},
                   ledger={"c0c03333": {"ok": True,
                                        "hours": [{"hour": "2026-10-17T00", "holder": "you"}]}})
    f = make_fleet(r, ["c0c03333"], claimed=[])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("未替换", f.cfg["verifiers"], ["c0c03333"])
    ck("未认领", r.claimed_calls, [])
    ck("返回 0", n, 0)
    ck("逐个归属行点名本地没记录", any("本地没有登记记录" in m for m in lines(f)), True)
    ck("逐个归属事件 local_claimed=False",
       ev(f, "verifier_ownership")["local_claimed"], False)


def test_unknown_is_not_claimed():
    print("归属未知 -> 保留，不冒认")
    r = FakeRemote(devices=("ours01",), points={"dddd4444": {"ok": False}})
    f = make_fleet(r, ["dddd4444"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("未替换", f.cfg["verifiers"], ["dddd4444"])
    ck("不认领", r.claimed_calls, [])
    ck("返回 0", n, 0)
    ck("提示归属未知", any("归属未知" in m for _, m in f.events), True)
    ck("逐个归属事件 state=unknown", ev(f, "verifier_ownership")["state"], "unknown")


def test_signed_out_skips():
    print("未登录 -> 整段跳过")
    r = FakeRemote(devices=("ours01",))
    f = make_fleet(r, ["eeee5555"], signed_in=False)
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("返回 0", n, 0)
    ck("不认领", r.claimed_calls, [])
    ck("没有逐个归属事件", f.log.events, [])


def test_claim_failure_is_survivable():
    print("认领失败 -> 不炸，记错误")
    r = FakeRemote(devices=("ours01",), claim_ok=False,
                   points={"ffff6666": {"ok": True, "claimed": False}})
    f = make_fleet(r, ["ffff6666"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("返回 0（没登记成功）", n, 0)
    ck("没有标记为已认领", f.verifiers[0].account_claimed, False)
    ck("失败有提示", any("归属登记失败" in m for _, m in f.events), True)


def test_held_by_prefix_is_not_stolen():
    print("已被持有（只靠前缀判定是自己的）-> 不冒认")
    r = FakeRemote(devices=("gggg77",),
                   points={"gggg7777": {"ok": True, "claimed": True}},
                   ledger={"gggg7777": {"ok": False}})
    f = make_fleet(r, ["gggg7777"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("未替换", f.cfg["verifiers"], ["gggg7777"])
    ck("不冒认", r.claimed_calls, [])
    ck("返回 0", n, 0)


def test_every_verifier_gets_one_line():
    print("三个 verifier（自己的 / 别人的 / 无主的）-> 三行，一个不少")
    r = FakeRemote(devices=("ours01",),
                   points={"aaaa1111": {"ok": True, "claimed": True},
                           "bbbb2222": {"ok": True, "claimed": True},
                           "cccc3333": {"ok": True, "claimed": False}},
                   ledger={"aaaa1111": {"ok": True, "hours": [{"hour": "h", "holder": "you"}]},
                           "bbbb2222": {"ok": True, "hours": [{"hour": "h", "holder": "other"}]}})
    f = make_fleet(r, ["aaaa1111", "bbbb2222", "cccc3333"], claimed=["aaaa1111"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("一个 verifier 一行", len(ev_all(f, "verifier_ownership")), 3)
    ck("正文：三个逐个行",
       len([m for m in lines(f) if re.match(r"^v\d\d [0-9a-f]+：", m)]), 3)
    ck("正文：开头一行", len([m for m in lines(f) if "逐个核对 3 个 verifier" in m]), 1)
    ck("正文：一行小结", len([m for m in lines(f) if "归属校验（启动）完成：共 3 个" in m]), 1)
    ck("返回登记数量 2（换掉的那个 + 无主的那个）", n, 2)
    summary = ev(f, "verifier_ownership_summary")
    ck("小结 total", summary["total"], 3)
    ck("小结 yours", summary["yours"], 1)
    ck("小结 foreign", summary["foreign"], 1)
    ck("小结 unowned", summary["unowned"], 1)
    ck("小结 unknown", summary["unknown"], 0)
    ck("小结 registered", summary["registered"], 2)
    ck("小结 replaced", summary["replaced"], 1)
    ck("小结 local_claimed", summary["local_claimed"], 1)
    ck("正文有小结行", any("归属校验（启动）完成：共 3 个" in m for m in lines(f)), True)
    ck("正文小结带四种结论",
       any("属于当前账号 1 · 无主 1 · 别人的 1 · 归属未知 0" in m for m in lines(f)), True)


def test_labels_are_numbered():
    print("逐个行按 v01/v02… 编号，和界面上的 ver 对得上")
    r = FakeRemote(devices=("ours01",), points={})
    f = make_fleet(r, ["aaaa1111", "bbbb2222"])
    asyncio.run(f.validate_verifier_ownership("启动"))
    labels = [e["label"] for e in ev_all(f, "verifier_ownership")]
    ck("编号", labels, ["v01", "v02"])
    ck("index 从 1 起", [e["index"] for e in ev_all(f, "verifier_ownership")], [1, 2])


def test_no_verifiers_is_sane():
    print("一个 verifier 都没有 -> 不炸，留一句话")
    r = FakeRemote(devices=("ours01",))
    f = make_fleet(r, [])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("返回 0", n, 0)
    ck("没有逐个归属事件", ev_all(f, "verifier_ownership"), [])
    ck("有说明", any("没有任何 verifier" in m for m in lines(f)), True)


def test_call_sites():
    print("调用点：启动 + ver 刷新")
    src = (ROOT / "src" / "fleet.py").read_text(encoding="utf-8")
    ck("启动时校验", 'await self.validate_verifier_ownership("启动")' in src, True)
    ck("刷新后校验", 'await self.validate_verifier_ownership("ver 刷新")' in src, True)
    ck("刷新失败不炸", "ver 刷新后的归属校验失败" in src, True)
    ck("有 _claim_unowned", "async def _claim_unowned(self, v: Verifier, reason: str)" in src, True)
    ck("旧签名已不存在", "async def validate_verifier_ownership(self) -> None:" in src, False)
    ck("每个 verifier 都有结构化事件", 'self.log.event("verifier_ownership",' in src, True)
    ck("有小结事件", 'self.log.event("verifier_ownership_summary",' in src, True)
    ck("小结带 registered", "registered=registered, replaced=len(replace)" in src, True)


if __name__ == "__main__":
    test_unowned_is_registered()
    test_foreign_is_switched()
    test_own_is_left_alone()
    test_own_but_not_recorded_locally()
    test_unknown_is_not_claimed()
    test_signed_out_skips()
    test_claim_failure_is_survivable()
    test_held_by_prefix_is_not_stolen()
    test_every_verifier_gets_one_line()
    test_labels_are_numbered()
    test_no_verifiers_is_sane()
    test_call_sites()
    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
