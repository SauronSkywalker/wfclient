# -*- coding: utf-8 -*-
"""Branch-B correctness gate.

1. The five stored reference rows against their known score/work — the same
   anchor every earlier core was checked on.
2. A batch of REAL rows taken from the client's own logs, compared
   bit-for-bit between two cores, at three lane counts (1 exercises the serial
   path, 4 and 6 exercise the chunked pipeline).

A mismatch is a failure; the point of the pipeline is that it changes WHEN the
additions happen, never WHICH ones or in what order.
"""
import ctypes, json, os, statistics, subprocess, sys, time, urllib.request

ROOT = r"D:\工程\zcode\wfsim"
LOG = os.path.join(ROOT, "client-portable-20261014-012150 (2)", "logs",
                   "events-20261015-175957.jsonl")
TMP = os.path.join(ROOT, "phase-build", "verify")
PORT_A, PORT_B = 9450, 9451
CPUS_A, CPUS_B = (0, 1, 2, 3, 4, 5), (6, 7, 8, 9, 10, 11)

REF = {
    "ocucor": (7.243395407715232, 3513660008, "standard_single_target", "base"),
    "dual_toxocyst": (162.83173857621276, 12175383036, "standard_single_target", "cycle"),
    "torid": (1.1397415477673707, 3680589703, "heavy_gunner", "base"),
    "boar_prime": (40.3613402902623, 26508498797, "standard_single_target", "base"),
    "burston_prime": (5.279499300241206, 3451569201, "heavy_gunner", "base"),
}
RECS = {
    "ocucor": {"arcanes": ["secondary_fortifier"], "evolutions": [], "exilus": "eject_magazine",
               "mods": ["frostbite", "riven", "primed_heated_charge", "pistol_acuity", "galvanized_shot",
                        "anemic_agility", "pistol_elementalist", "augur_seeker"],
               "riven_neg": "status_duration", "riven_pos": ["corrosive", "electricity", "projectile_speed"],
               "riven_rolls": [1.1, 1.1, 1.1, 0.9], "weapon": "ocucor"},
    "dual_toxocyst": {"arcanes": ["secondary_deadhead"],
                      "evolutions": ["dual_toxocyst_evo1_incarnon_form", "dual_toxocyst_fevered_frenzy",
                                     "dual_toxocyst_ready_retaliation", "dual_toxocyst_commodores_fortune"],
                      "exilus": "eject_magazine",
                      "mods": ["primed_convulsion", "galvanized_diffusion", "primed_target_cracker",
                               "galvanized_crosshairs", "galvanized_shot", "lethal_torrent",
                               "anemic_agility", "riven"],
                      "riven_neg": "status_duration", "riven_pos": ["damage", "puncture", "weak_point_damage"],
                      "riven_rolls": [1.1, 1.1, 1.1, 0.9], "weapon": "dual_toxocyst"},
    "torid": {"arcanes": ["primary_deadhead"],
              "evolutions": ["torid_evo1_incarnon_form", "torid_final_fusillade", "torid_swift_deliverance",
                             "torid_commodores_fortune"],
              "exilus": "vigilante_supplies",
              "mods": ["riven", "galvanized_chamber", "galvanized_aptitude", "galvanized_scope",
                       "vile_acceleration", "vital_sense", "hunter_track@4", "magnetic_capacity"],
              "riven_neg": "status_duration", "riven_pos": ["electricity", "reload_speed", "viral"],
              "riven_rolls": [1.1, 1.1, 1.1, 1.1], "weapon": "torid"},
    "boar_prime": {"arcanes": ["primary_deadhead"],
                   "evolutions": ["boar_prime_evo1_incarnon_form", "boar_prime_reified_bane",
                                  "boar_prime_ready_retaliation", "boar_prime_critical_parallel"],
                   "exilus": "vigilante_supplies",
                   "mods": ["primed_charged_shell", "toxic_barrage", "galvanized_hell", "primed_ravage",
                            "critical_deceleration", "hunter_track", "magnetic_strafe", "riven"],
                   "riven_neg": "status_duration", "riven_pos": ["fire_rate", "gas", "multishot"],
                   "riven_rolls": [1.1, 1.1, 1.1, 0.9], "weapon": "boar_prime"},
    "burston_prime": {"arcanes": ["primary_deadhead"],
                      "evolutions": ["burston_prime_evo1_incarnon_form", "burston_prime_forceful_finality",
                                     "burston_prime_extended_volley", "burston_prime_absolute_valor"],
                      "exilus": "vigilante_supplies",
                      "mods": ["primed_cryo_rounds", "riven", "primary_acuity", "galvanized_scope",
                               "bladed_rounds", "vital_sense", "gilded_truth", "continuous_misery@2"],
                      "riven_neg": "status_duration", "riven_pos": ["fire_rate", "reload_speed", "toxin"],
                      "riven_rolls": [1.1, 1.1, 1.1, 0.9], "weapon": "burston_prime"},
}
N_REAL = 12
LANES = (1, 4, 6)


def pin(pid, cpus):
    mask = sum(1 << c for c in cpus)
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1F0FFF, False, pid) or k32.OpenProcess(0x0400 | 0x0100, False, pid)
    k32.SetProcessAffinityMask(h, ctypes.c_size_t(mask)); k32.CloseHandle(h)


def post(port, body, timeout=1800):
    rq = urllib.request.Request("http://127.0.0.1:%d/api/board/row" % port,
                                data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode())


def real_rows(n):
    body = {}
    for line in open(LOG, encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("kind") == "engine_request" and o.get("endpoint") == "/api/board/row":
            b = o.get("body")
            if b:
                body[(b["record"]["weapon"], b["ruler"], b["mode"])] = b
    keys = sorted(body)
    step = max(1, len(keys) // n)
    return [body[k] for k in keys[::step][:n]]


def sig(out):
    return (out.get("score"), out.get("work"), out.get("metric"),
            json.dumps(out.get("counts") or {}, sort_keys=True))


def start(exe, port, cpus, logpath):
    env = dict(os.environ, WFSIM_PORT=str(port))
    fh = open(logpath, "wb")
    p = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=fh)
    pin(p.pid, cpus)
    for _ in range(240):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/meta" % port, timeout=5).read()
            return p, fh
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("engine %s did not come up" % exe)


def main():
    os.makedirs(TMP, exist_ok=True)
    exe_a = sys.argv[1]      # baseline core (fold, ad12f977)
    exe_b = sys.argv[2]      # branch-B core (pipeline)
    print("baseline : %s" % exe_a)
    print("branch B : %s" % exe_b)
    pa, fa = start(exe_a, PORT_A, CPUS_A, os.path.join(TMP, "base.log"))
    pb, fb = start(exe_b, PORT_B, CPUS_B, os.path.join(TMP, "pipe.log"))
    fails = 0
    try:
        print("\n=== 1. five stored reference rows ===")
        for w, (rs, rw, ruler, mode) in REF.items():
            body = {"record": RECS[w], "ruler": ruler, "mode": mode, "threads": 6}
            ra = post(PORT_A, body)
            rb = post(PORT_B, body)
            good = (ra.get("score") == rs and ra.get("work") == rw)
            same = sig(ra) == sig(rb)
            if not (good and same):
                fails += 1
            print("  %-15s ref=%r/%d  A=%r/%d  B=%r/%d  %s"
                  % (w, rs, rw, ra.get("score"), ra.get("work"), rb.get("score"), rb.get("work"),
                     "OK" if good and same else "*** MISMATCH ***"))

        rows = real_rows(N_REAL)
        print("\n=== 2. %d real rows x lanes %s, bit-for-bit A vs B ===" % (len(rows), LANES))
        for lanes in LANES:
            bad = 0
            t_a = t_b = 0.0
            for b in rows:
                body = dict(b, threads=lanes)
                t0 = time.perf_counter(); ra = post(PORT_A, body); t_a += time.perf_counter() - t0
                t0 = time.perf_counter(); rb = post(PORT_B, body); t_b += time.perf_counter() - t0
                if sig(ra) != sig(rb):
                    bad += 1
                    if bad <= 2:
                        print("    DIFF lanes=%d %s: A=%s B=%s"
                              % (lanes, b["record"]["weapon"], sig(ra)[:2], sig(rb)[:2]))
            fails += bad
            print("  lanes=%d : %d rows, %d different   (A %.1fs, B %.1fs)"
                  % (lanes, len(rows), bad, t_a, t_b))
    finally:
        for p, fh in ((pa, fa), (pb, fb)):
            try:
                p.terminate(); p.wait(timeout=10)
            except Exception:
                try: p.kill()
                except Exception: pass
            try: fh.close()
            except Exception: pass
    print("\n%s" % ("ALL BIT-IDENTICAL" if fails == 0 else "FAILURES: %d" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
