"""Fold the wasm-recomputed shards back into the frozen baseline.

`ci/tools/wasm_baseline.mjs` recomputes, in parallel shards, what the site's
wasm says the answers are.  This script writes those numbers into the three
baseline files the gates read:

    corpus.json        100 board rows  -> expected.score/metric/work/counts
    riven3.json          3 riven rows  -> expected.result.work/final_work/results
    riven_corpus.json   20 riven rows  -> same fields
    anchors.json        (new) the five site-wasm anchor rows, which
                        verify_pipeline.py prefers over its built-in REF table

Only the fields the gates actually compare are touched: `score`/`metric` cannot
move (physics does not depend on the gas table) while `work`/`counts.gas` do,
and `results`/`final_work` follow.  Everything else in a frozen row (the request,
the note, `builds`, `fights`, `searched`) is left exactly as it was — those are
search-shape facts that the gas table does not change.

    python ci/tools/apply_baseline.py --parts C:/wfsim-build/base --repo .
    python ci/tools/apply_baseline.py --parts <dir> --report <dir>/apply.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent
REAL = BASE

# Set from --base before anything is written; tests point it at a copy so a
# partial set of shards can be rehearsed without touching the real baseline.
def _files(base: pathlib.Path) -> dict:
    return {
        "board": base / "corpus.json",
        "riven3": base / "riven3.json",
        "riven_corpus": base / "riven_corpus.json",
    }


FILES = _files(BASE)


def load(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: pathlib.Path, doc) -> None:
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def index(rows: list) -> dict:
    return {it["id"]: it for it in rows}


def collect(parts: pathlib.Path) -> tuple[dict, list]:
    """Read every shard, refusing to mix two different wasm revisions."""
    got: dict[str, dict] = {"board": {}, "riven": {}, "anchors": {}}
    meta: list = []
    for f in sorted(parts.glob("*.json")):
        if f.name.startswith("apply"):
            continue
        doc = load(f)
        mode = doc.get("mode")
        if mode not in got:
            print(f"  skip {f.name}: not a baseline part (mode={mode!r})")
            continue
        stamp = (doc.get("wasm"), doc.get("engine"), doc.get("release"))
        if meta and stamp != (meta[0]["wasm"], meta[0]["engine"], meta[0]["release"]):
            raise SystemExit(
                f"!! {f.name} was computed from {stamp} but {meta[0]['file']} "
                f"from {(meta[0]['wasm'], meta[0]['engine'], meta[0]['release'])}")
        meta.append({"file": f.name, "mode": mode, "wasm": stamp[0],
                     "engine": stamp[1], "release": stamp[2], "rows": len(doc.get("rows") or {})})
        if mode == "anchors":
            got["anchors"].update(doc.get("rows") or {})
        elif mode == "board":
            got["board"].update(doc.get("rows") or {})
        elif mode == "riven":
            # A riven shard may hold rows for either riven file; both are
            # matched by id below, so no distinction is needed here.
            got["riven"].update(doc.get("rows") or {})
    return got, meta


def find(part_meta: list, mode: str) -> pathlib.Path | None:
    names = {m["file"] for m in part_meta if m["mode"] == mode}
    if not names:
        return None
    return BASE / ("corpus.json" if mode == "board" else "riven3.json")


def apply_board(rows: dict) -> dict:
    path = FILES["board"]
    items = load(path)
    changed, missing = 0, 0
    for it in items:
        got = rows.get(it["id"])
        if not got:
            missing += 1
            continue
        exp = it.setdefault("expected", {})
        before = (exp.get("work"), json.dumps(exp.get("counts"), sort_keys=True))
        exp["score"] = got["score"]
        exp["metric"] = got["metric"]
        exp["work"] = got["work"]
        if got.get("counts") is not None:
            exp["counts"] = got["counts"]
        if before != (exp.get("work"), json.dumps(exp.get("counts"), sort_keys=True)):
            changed += 1
        if float(exp["score"]) != float(got["score"]):
            raise SystemExit(f"!! {it['id']}: the wasm moved the SCORE, that is not a gas drift")
    if missing:
        print(f"  board: {missing} of {len(items)} rows were not recomputed — left as they were")
    dump(path, items)
    return {"file": path.name, "rows": len(items), "updated": changed, "skipped": missing}


def apply_riven(name: str, rows: dict) -> dict | None:
    path = FILES[name]
    if not path.exists():
        return None
    items = load(path)
    changed, missing, dropped = 0, 0, 0
    for it in items:
        got = rows.get(it["id"])
        if not got:
            missing += 1
            continue
        exp = it.setdefault("expected", {}).setdefault("result", {})
        before = (exp.get("work"), exp.get("final_work"))
        if "work" in got:
            exp["work"] = got["work"]
        elif "work" in exp:
            # The site's wasm returns the optimizer's own report, which carries
            # `final_work` but NOT the door-level `work` (the door sums
            # `search_work + final_work`, and `search_work` never leaves the
            # native door).  A stale `work` would fail every run, so the key
            # goes away; `final_work` plus the full `results` array — every
            # winner's build, score and work — still pin the answer bit for bit.
            del exp["work"]
            dropped += 1
        exp["final_work"] = got["final_work"]
        exp["results"] = got["results"]
        if before != (exp.get("work"), exp.get("final_work")):
            changed += 1
    if missing:
        print(f"  {path.name}: {missing} of {len(items)} rows were not recomputed — left as they were")
    if dropped:
        print(f"  {path.name}: dropped the stale door-level `work` on {dropped} rows")
    dump(path, items)
    return {"file": path.name, "rows": len(items), "updated": changed, "skipped": missing}


def apply_anchors(rows: dict) -> dict:
    """anchors.json is the authority verify_pipeline.py reads; anchor-rows.json
    keeps the records and is the input of the recompute."""
    out = BASE / "anchors.json"
    doc = load(out) if out.exists() else {"rows": {}}
    doc.setdefault("rows", {})
    changed = 0
    records = load(BASE / "anchor-rows.json").get("anchors") or {}
    for weapon, got in rows.items():
        if weapon not in records:
            raise SystemExit(f"!! anchors.json got an unknown row {weapon!r}")
        old = doc["rows"].get(weapon) or {}
        if old.get("work") != got["work"] or float(old.get("score", -1)) != float(got["score"]):
            changed += 1
        doc["rows"][weapon] = {"score": got["score"], "work": got["work"]}
    doc["wasm"] = doc.get("wasm")
    dump(out, doc)
    return {"file": out.name, "rows": len(doc["rows"]), "updated": changed, "skipped": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", required=True, help="directory holding the wasm_baseline.mjs outputs")
    ap.add_argument("--report", default="", help="write a JSON summary here")
    ap.add_argument("--base", default="", help="baseline directory (default: the one next to this script)")
    a = ap.parse_args()

    global BASE, FILES
    if a.base:
        BASE = pathlib.Path(a.base).resolve()
        FILES = _files(BASE)
        print("baseline ->", BASE)

    parts = pathlib.Path(a.parts)
    rows, meta = collect(parts)
    if not meta:
        raise SystemExit(f"!! no baseline parts found in {parts}")
    print("wasm %s engine %s release %s" % (meta[0]["wasm"], meta[0]["engine"], meta[0]["release"]))
    for m in meta:
        print("  %-18s %-8s %3d rows" % (m["file"], m["mode"], m["rows"]))

    report: dict = {"wasm": meta[0]["wasm"], "engine": meta[0]["engine"],
                    "release": meta[0]["release"], "parts": meta, "files": []}
    if rows["board"]:
        report["files"].append(apply_board(rows["board"]))
    else:
        print("  (no board shards: corpus.json untouched)")
    if rows["riven"]:
        for name in ("riven3", "riven_corpus"):
            got = apply_riven(name, rows["riven"])
            if got:
                report["files"].append(got)
    else:
        print("  (no riven shards: the riven corpora untouched)")
    if rows["anchors"]:
        report["files"].append(apply_anchors(rows["anchors"]))
    else:
        print("  (no anchor shard: anchors.json untouched)")

    for f in report["files"]:
        print("  %-20s updated %3d / %3d rows" % (f["file"], f["updated"], f["rows"]))
    if a.report:
        pathlib.Path(a.report).write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
        print("  report -> %s" % a.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
