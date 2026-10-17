"""发货包：ver 归属校验（用户要求 m06136）

启动时、以及 ver 集合每次变更（refresh_verifiers）后都要再校验一遍：
  * 无主的 verifier          -> 立刻登记到当前账号名下；
  * 属于别的账号             -> 换成一个新的无主 id（自动切换）；
  * 属于当前账号 / 归属未知  -> 不动。

用法：python test_verifier_owner.py <包目录>
"""
import asyncio
import pathlib
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


def make_fleet(remote, verifiers, *, signed_in=True):
    f = object.__new__(F.Fleet)
    f.remote = remote
    f.cfg = {"verifiers": list(verifiers), "max_verifiers": len(verifiers) or 6,
             "claimed": [], "active_verifiers": len(verifiers) or 6,
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
    ck("日志里 tasks_held=0", f.log.events[0][1]["tasks_held"], 0)
    ck("日志里带 reason", f.log.events[0][1]["reason"], "启动")
    ck("on_event 有提示", any("登记到当前账号" in m for _, m in f.events), True)


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


def test_own_is_left_alone():
    print("自己的 -> 不动")
    r = FakeRemote(devices=("cccc33",),
                   points={"cccc3333": {"ok": True, "claimed": True}},
                   ledger={"cccc3333": {"ok": True,
                                        "hours": [{"hour": "2026-10-17T00", "holder": "you"}]}})
    f = make_fleet(r, ["cccc3333"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("未替换", f.cfg["verifiers"], ["cccc3333"])
    ck("未重复认领", r.claimed_calls, [])
    ck("返回登记数量", n, 0)


def test_unknown_is_not_claimed():
    print("归属未知 -> 保留，不冒认")
    r = FakeRemote(devices=("ours01",), points={"dddd4444": {"ok": False}})
    f = make_fleet(r, ["dddd4444"])
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("未替换", f.cfg["verifiers"], ["dddd4444"])
    ck("不认领", r.claimed_calls, [])
    ck("返回 0", n, 0)
    ck("提示归属未知", any("归属未知" in m for _, m in f.events), True)


def test_signed_out_skips():
    print("未登录 -> 整段跳过")
    r = FakeRemote(devices=("ours01",))
    f = make_fleet(r, ["eeee5555"], signed_in=False)
    n = asyncio.run(f.validate_verifier_ownership("启动"))
    ck("返回 0", n, 0)
    ck("不认领", r.claimed_calls, [])


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


def test_call_sites():
    print("调用点：启动 + ver 刷新")
    src = (ROOT / "src" / "fleet.py").read_text(encoding="utf-8")
    ck("启动时校验", 'await self.validate_verifier_ownership("启动")' in src, True)
    ck("刷新后校验", 'await self.validate_verifier_ownership("ver 刷新")' in src, True)
    ck("刷新失败不炸", "ver 刷新后的归属校验失败" in src, True)
    ck("有 _claim_unowned", "async def _claim_unowned(self, v: Verifier, reason: str)" in src, True)
    ck("旧签名已不存在", "async def validate_verifier_ownership(self) -> None:" in src, False)


if __name__ == "__main__":
    test_unowned_is_registered()
    test_foreign_is_switched()
    test_own_is_left_alone()
    test_unknown_is_not_claimed()
    test_signed_out_skips()
    test_claim_failure_is_survivable()
    test_held_by_prefix_is_not_stolen()
    test_call_sites()
    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
