# -*- coding: utf-8 -*-
"""标准语料运行器：起一个引擎，跑完 bench/corpus.json，逐位比对 + 计时。

逐位口径：`score`（浮点精确相等）、`work`（整数）、`metric`、`counts`（字典全等）。
任何一条不等即判 DIFF，退出码 1。

用法：
  python bench/run_corpus.py <engine.exe> [--lanes 6] [--passes 1] [--cpus 0,1,2,3,4,5]
                             [--corpus bench/corpus.json] [--anchors]
"""
import argparse, json, os, subprocess, sys, time, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)


def pin(pid, cpus):
    if not cpus:
        return
    mask = 0
    for c in cpus:
        mask |= 1 << int(c)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "(Get-Process -Id %d).ProcessorAffinity=%d" % (pid, mask)],
                   capture_output=True)


def wait_port(port, secs=90):
    for _ in range(secs * 4):
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, errors="replace").stdout
        if any((":%d " % port) in ln and "LISTEN" in ln.upper()
               for ln in out.splitlines()):
            return True
        time.sleep(0.25)
    return False


def post(port, body, timeout=1800, path="/api/board/row"):
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode())


def same(got, want):
    """Bit-for-bit on the three things the protocol compares."""
    out = []
    if float(got.get("score", float("nan"))) != float(want["score"]):
        out.append("score %r != %r" % (got.get("score"), want["score"]))
    if str(got.get("metric")) != str(want["metric"]):
        out.append("metric %r != %r" % (got.get("metric"), want["metric"]))
    if int(got.get("work") or 0) != int(want["work"]):
        out.append("work %r != %r" % (got.get("work"), want["work"]))
    if (got.get("counts") or {}) != (want.get("counts") or {}):
        g, w = got.get("counts") or {}, want.get("counts") or {}
        bad = [k for k in set(g) | set(w) if g.get(k) != w.get(k)]
        out.append("counts differ on %s" % ",".join(sorted(bad)[:5]))
    return out


def run_riven(port, entries, timeout=600.0):
    """回放 riven 语料：POST /api/optimize + 轮询 /api/optimize/status。

    比对口径：`work` / `final_work`（整数，最硬），以及冻结报告里 `builds`/
    `results` 的优胜项——riven 的协议 canon 是 `canon({build, score, work})`，
    顶层并没有 build/score（那是优化器的整份报告）。
    """
    bad = 0
    for it in entries:
        body = dict(it["payload"]["request"], threads=it.get("lanes", 6))
        t0 = time.time()
        try:
            started = post(port, body, path="/api/optimize", timeout=120)
        except Exception as e:
            print("  %-14s TRANSPORT %s" % (it["id"], e)); bad += 1; continue
        res = started.get("result") if isinstance(started, dict) else None
        job = started.get("job_id") if isinstance(started, dict) else None
        while res is None and job is not None and time.time() - t0 < timeout:
            time.sleep(0.5)
            try:
                st = post(port, {"id": job}, path="/api/optimize/status", timeout=10)
            except Exception:
                continue
            if isinstance(st, dict) and st.get("result") is not None:
                res = st["result"]
            elif isinstance(st, dict) and (st.get("error") or st.get("ok") is False):
                break
        dt = time.time() - t0
        if res is None:
            print("  %-14s FAIL no result (%.0fs)" % (it["id"], dt)); bad += 1; continue
        want = (it["expected"] or {}).get("result") or {}
        diff = []
        for k in ("work", "final_work"):
            if k in want and res.get(k) != want[k]:
                diff.append("%s %r != %r" % (k, res.get(k), want[k]))
        if want.get("builds") is not None and res.get("builds") != want["builds"]:
            diff.append("builds differ")
        if want.get("results") is not None and res.get("results") != want["results"]:
            diff.append("results differ")
        if diff:
            bad += 1
            print("  %-14s DIFF %s" % (it["id"], "; ".join(diff)[:120]))
        else:
            print("  %-14s ok %7.0fs" % (it["id"], dt))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("--lanes", type=int, default=6)
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--cpus", default="")
    ap.add_argument("--stderr", default="",
                    help="把引擎 stderr 落盘（ALLOCSPAN / PHASE 诊断都在这里；"
                         "默认丢弃，因为语料校验本身不需要）")
    ap.add_argument("--corpus", default=os.path.join(BASE, "corpus.json"))
    ap.add_argument("--anchors", action="store_true")
    ap.add_argument("--riven", action="store_true",
                    help="也回放 riven_corpus.json（20 条约 4–5 分钟；--riven-corpus 可换子集）")
    ap.add_argument("--riven-corpus", default=os.path.join(BASE, "riven_corpus.json"),
                    help="紫卡语料路径（默认 ci/tools/riven_corpus.json；快筛用 riven3.json）")
    ap.add_argument("--port", type=int, default=9398)
    a = ap.parse_args()

    entries = json.load(open(a.corpus, encoding="utf-8"))
    riven = []
    if a.riven:
        rp = a.riven_corpus
        if os.path.exists(rp):
            riven = json.load(open(rp, encoding="utf-8"))
            print("riven corpus %d rows (about %.1f minutes)"
                  % (len(riven), sum(x["note"].get("build_s", 60) for x in riven) / 60.0))
        else:
            print("no %s, skipping riven" % rp)
    if a.anchors:
        from verify_pipeline import REF, RECS
        for w, (score, work, ruler, mode) in REF.items():
            entries.append({"id": "ANCHOR-" + w, "kind": "board", "lanes": a.lanes,
                            "payload": {"record": RECS[w], "ruler": ruler, "mode": mode},
                            "expected": {"score": score, "work": work,
                                         "metric": None, "counts": None},
                            "note": {"weapon": w, "ruler": ruler, "mode": mode}})
    print("corpus %d rows, lanes=%d, passes=%d" % (len(entries), a.lanes, a.passes))

    env = dict(os.environ, WFSIM_PORT=str(a.port))
    errf = open(a.stderr, "w", encoding="utf-8", errors="replace") if a.stderr else None
    p = subprocess.Popen([a.engine], env=env, stdout=subprocess.DEVNULL,
                         stderr=errf if errf else subprocess.DEVNULL)
    bad = 0
    try:
        if not wait_port(a.port):
            print("engine never listened"); return 1
        pin(p.pid, [c for c in a.cpus.split(",") if c != ""])
        for pno in range(1, a.passes + 1):
            total = 0.0
            for it in entries:
                body = dict(it["payload"], threads=it.get("lanes", a.lanes))
                t0 = time.time()
                try:
                    got = post(a.port, body)
                except urllib.error.URLError as e:
                    print("  %-26s TRANSPORT %s" % (it["id"], e)); bad += 1; continue
                ms = (time.time() - t0) * 1000
                total += ms
                if not got.get("ok"):
                    print("  %-26s FAIL %s" % (it["id"], str(got)[:80])); bad += 1; continue
                exp = it["expected"]
                if exp.get("metric") is None and exp.get("counts") is None:
                    # anchors: only score/work are frozen
                    diff = []
                    if float(got.get("score")) != float(exp["score"]):
                        diff.append("score %r != %r" % (got.get("score"), exp["score"]))
                    if int(got.get("work") or 0) != int(exp["work"]):
                        diff.append("work %r != %r" % (got.get("work"), exp["work"]))
                else:
                    diff = same(got, exp)
                if diff:
                    bad += 1
                    print("  %-26s DIFF %s" % (it["id"], "; ".join(diff)))
                else:
                    print("  %-26s ok %7.0fms" % (it["id"], ms))
            print("pass %d board: total %.1f s, %d rows" % (pno, total / 1000.0, len(entries)))
            if riven:
                t0 = time.time()
                bad += run_riven(a.port, riven)
                print("pass %d riven: %.1f s, %d rows" % (pno, time.time() - t0, len(riven)))
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
    print("\n%s: %d mismatches" % ("PASS" if bad == 0 else "FAIL", bad))
    return 1 if bad else 0


sys.exit(main())
