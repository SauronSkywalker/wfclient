#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验 2.2 批量路由：/api/board/rows 的结果必须与逐条 /api/board/row 逐位一致。

引擎侧的批量路由把「解析 order[k+1] 与折叠 fold[k]」重叠起来，折叠仍然严格串行
（一次只有一行在折叠，避免两行同时向 lane 池要满线程）。它必须**不改变任何数值**：
本脚本对同一批语料先逐条算一遍、再用一次批量调用算一遍，用与 run_corpus 相同的
口径（score/metric/work/counts 全等）逐行比对，并报告两条路径的墙钟。

自带 post/wait_port/same 三个小助手（不从 run_corpus 导入：那个文件在模块级就调用
main()，一 import 就会跑它的 argparse）。

用法：python bench/verify_batch.py <引擎exe> [--n 8] [--lanes 6] [--port 9421]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def wait_port(port: int, secs: int = 90) -> bool:
    for _ in range(secs * 4):
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, errors="replace").stdout
        if any((":%d " % port) in ln and "LISTEN" in ln.upper()
               for ln in out.splitlines()):
            return True
        time.sleep(0.25)
    return False


def post(port: int, body: dict, timeout: int = 1800, path: str = "/api/board/row"):
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode())


def same(got: dict, want: dict) -> list:
    """与 run_corpus.same() 同口径：score/metric/work/counts 全等。"""
    out = []
    if float(got.get("score", float("nan"))) != float(want["score"]):
        out.append("score %r != %r" % (got.get("score"), want["score"]))
    if str(got.get("metric")) != str(want["metric"]):
        out.append("metric %r != %r" % (got.get("metric"), want["metric"]))
    if int(got.get("work") or 0) != int(want["work"]):
        out.append("work %r != %r" % (got.get("work"), want["work"]))
    if (got.get("counts") or {}) != (want.get("counts") or {}):
        g, w = got.get("counts") or {}, want.get("counts") or {}
        bad = sorted(k for k in set(g) | set(w) if g.get(k) != w.get(k))
        out.append("counts differ on %s" % ",".join(bad[:4]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--lanes", type=int, default=6)
    ap.add_argument("--port", type=int, default=9421)
    ap.add_argument("--corpus", default=os.path.join(BASE, "corpus.json"))
    a = ap.parse_args()

    entries = json.load(open(a.corpus, encoding="utf-8"))[:a.n]
    env = dict(os.environ, WFSIM_PORT=str(a.port))
    p = subprocess.Popen([a.engine], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    bad = 0
    try:
        if not wait_port(a.port):
            print("engine never listened")
            return 1

        # 1) 逐条：N 次 /api/board/row
        t0 = time.time()
        one = []
        for it in entries:
            one.append(post(a.port, dict(it["payload"],
                                         threads=it.get("lanes", a.lanes))))
        t_one = time.time() - t0

        # 2) 一批：1 次 /api/board/rows
        rows = [dict(it["payload"]) for it in entries]
        t0 = time.time()
        got = post(a.port, {"threads": a.lanes, "rows": rows},
                   path="/api/board/rows")
        t_batch = time.time() - t0

        if not got.get("ok"):
            print("批量调用失败:", str(got)[:300])
            return 1
        res = got.get("results")
        if not isinstance(res, list) or len(res) != len(rows):
            print("results 不是等长列表:", type(res).__name__,
                  len(res) if isinstance(res, list) else "-")
            return 1

        for it, o, b in zip(entries, one, res):
            if not o.get("ok"):
                print("  %-14s 单条失败 %s" % (it["id"], str(o)[:70]))
                bad += 1
                continue
            if not b.get("ok"):
                print("  %-14s 批量失败 %s" % (it["id"], str(b)[:70]))
                bad += 1
                continue
            diff = same(b, {"score": o.get("score"), "metric": o.get("metric"),
                            "work": o.get("work"), "counts": o.get("counts")})
            if diff:
                bad += 1
                print("  %-14s 批量与单条不一致：%s" % (it["id"], "; ".join(diff)))
            else:
                print("  %-14s 一致 score=%s work=%s metric=%s"
                      % (it["id"], b.get("score"), b.get("work"), b.get("metric")))

        print("逐条 %d 行 %.1f 秒；一批 %d 行 %.1f 秒；B/S %.3f；不一致 %d"
              % (len(one), t_one, len(rows), t_batch, t_batch / t_one, bad))
    finally:
        p.terminate()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
