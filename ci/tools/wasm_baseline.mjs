// Recompute the frozen baseline with the SITE'S OWN WASM.
//
// The wasm in `site/pkg` is what wfsim.app runs in the visitor's browser, so it
// is the standard answer for "what does the site compute".  When upstream
// changes the gas table, the engine's `work` moves with it and our frozen
// baseline goes stale — the gates then fail against an authority that is itself
// out of date.  This tool re-derives the baseline from the new wasm, in shards
// so the CI can run four of them at once.
//
//   node ci/tools/wasm_baseline.mjs --anchors  --out part-anchors.json
//   node ci/tools/wasm_baseline.mjs --board  --file ci/tools/corpus.json --shard 0/4 --out part-board-0.json
//   node ci/tools/wasm_baseline.mjs --riven  --file ci/tools/riven_corpus.json --shard 0/4 --out part-riven-0.json
//   node ci/tools/wasm_baseline.mjs --probe  --out probe.json       # 5 items, no writes
//
// --site <dir>  the upstream checkout's `site` folder (default ../upstream/site)
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

function arg(name, dflt = "") {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--")
    ? process.argv[i + 1] : (i >= 0 ? "true" : dflt);
}
const has = (name) => process.argv.includes("--" + name);

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SITE = path.resolve(arg("site", path.resolve(HERE, "..", "..", "upstream", "site")));

function loadEngine() {
  const rel = JSON.parse(fs.readFileSync(path.join(SITE, "release.json"), "utf8"));
  const js = path.join(SITE, "pkg", `wfsim_wasm.${rel.wasm}.js`);
  const wasm = path.join(SITE, "pkg", `wfsim_wasm_bg.${rel.wasm}.wasm`);
  for (const f of [js, wasm]) {
    if (!fs.existsSync(f)) throw new Error(`missing ${f} (was the site folder checked out?)`);
  }
  const ctx = vm.createContext({ console, TextEncoder, TextDecoder, WebAssembly });
  vm.runInContext(fs.readFileSync(js, "utf8") + "\nthis.wasm_bindgen = wasm_bindgen;", ctx);
  ctx.bytes = fs.readFileSync(wasm);
  vm.runInContext("wasm_bindgen.initSync({ module: bytes })", ctx);
  return { rel, ctx, api: (p, b) => JSON.parse(ctx.wasm_bindgen.api(p, JSON.stringify(b))) };
}

// The page's own path (site/asset/*-board-work.js measureRow): order one request,
// then feed it chunk by chunk through runs -> fold, and score the accumulator.
function boardRow(api, row) {
  const t0 = Date.now();
  const order = api("/api/board/order", { record: row.record, ruler: row.ruler, mode: row.mode });
  if (!order.ok) throw new Error("board/order: " + JSON.stringify(order));
  const request = order.request;
  const runs = Number(request.runs) || 0;
  let acc = null;
  for (let from = 0; from < runs; from += 1) {
    const got = api("/api/board/runs", { request, from, count: 1 });
    if (!got.ok) throw new Error("board/runs: " + JSON.stringify(got));
    const step = api("/api/board/fold", { request, acc, pieces: got.shards });
    if (!step.ok) throw new Error("board/fold: " + JSON.stringify(step));
    acc = step.acc;
  }
  const done = api("/api/board/score", { ruler: row.ruler, request, acc });
  return {
    score: done.score, metric: done.metric, work: done.work, counts: done.counts,
    runs, seconds: Math.round((Date.now() - t0) / 100) / 10,
  };
}

// The optimizer is not one of the `api` doors: the glue exports optimize(...)
// and site/asset/worker.*.js calls exactly this.
function rivenRow(ctx, request, lanes) {
  const body = JSON.parse(JSON.stringify(request));
  const t0 = Date.now();
  const out = JSON.parse(ctx.wasm_bindgen.optimize(JSON.stringify(body), () => {}, () => {}, () => {}));
  const secs = Math.round((Date.now() - t0) / 100) / 10;
  // The door echoes the whole request back inside each winner's `replay`,
  // including the thread count the caller sent; the wasm has no threads, so the
  // echo has to be restored to what run_corpus.py sends, or every comparison
  // would differ on a field that says nothing about the computation.
  for (const r of (out.results || [])) {
    if (r && r.replay && typeof r.replay === "object") r.replay.threads = lanes;
  }
  return { work: out.work, final_work: out.final_work, results: out.results, seconds: secs };
}

function readJSON(p) {
  return JSON.parse(fs.readFileSync(p, "utf8"));
}
function pickShard(items, shard) {
  if (!shard) return items;
  const [i, n] = shard.split("/").map(Number);
  return items.filter((_, k) => k % n === i);
}
function writeOut(name, doc) {
  const out = arg("out", path.join(HERE, name));
  fs.mkdirSync(path.dirname(path.resolve(out)), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(doc, null, 1));
  console.log("wrote %s (%d KB)", out, Math.round(fs.statSync(out).size / 1024));
  return out;
}

const engine = loadEngine();
const stamp = {
  wasm: engine.rel.wasm, engine: engine.rel.engine, release: engine.rel.release,
  protocol: engine.rel.protocol, at: new Date().toISOString(),
};
console.log("wasm %s engine %s release %s", stamp.wasm, stamp.engine, stamp.release);

if (has("anchors")) {
  const rows = readJSON(arg("file", path.join(HERE, "anchor-rows.json"))).anchors;
  const out = {};
  for (const [weapon, row] of Object.entries(rows)) {
    const got = boardRow(engine.api, { record: row.record, ruler: row.ruler, mode: row.mode });
    out[weapon] = { score: got.score, work: got.work, seconds: got.seconds };
    console.log("  anchor " + weapon.padEnd(16) + " score=" + got.score + " work=" + got.work + " (" + got.seconds + "s)");
  }
  writeOut("anchors.json", { ...stamp, mode: "anchors", rows: out });
} else if (has("board")) {
  const file = arg("file", path.join(HERE, "corpus.json"));
  const items = pickShard(readJSON(file), arg("shard"));
  const out = {};
  for (const it of items) {
    const got = boardRow(engine.api, it.payload);
    out[it.id] = { score: got.score, metric: got.metric, work: got.work,
                   counts: got.counts, seconds: got.seconds };
    console.log("  %s score=%s work=%s (%ss)", it.id, got.score, got.work, got.seconds);
  }
  writeOut(path.basename(file).replace(/\.json$/, "") + ".json", { ...stamp, mode: "board", rows: out });
} else if (has("riven")) {
  const file = arg("file", path.join(HERE, "riven3.json"));
  const items = pickShard(readJSON(file), arg("shard"));
  const out = {};
  for (const it of items) {
    const got = rivenRow(engine.ctx, it.payload.request, it.lanes || 6);
    out[it.id] = { work: got.work, final_work: got.final_work, results: got.results,
                   seconds: got.seconds };
    console.log("  %s work=%s final_work=%s results=%d (%ss)",
      it.id, got.work, got.final_work, (got.results || []).length, got.seconds);
  }
  writeOut(path.basename(file).replace(/\.json$/, "") + ".json", { ...stamp, mode: "riven", rows: out });
} else if (has("probe")) {
  // Five items, one per kind, chosen deterministically from the wasm hash so a
  // repeated probe asks the same question while a new wasm asks a new one.
  const seed = parseInt(crypto.createHash("sha1").update(stamp.wasm).digest("hex").slice(0, 8), 16);
  const corpus = readJSON(path.join(HERE, "corpus.json"));
  const rivens = readJSON(path.join(HERE, "riven3.json"));
  const anchors = readJSON(path.join(HERE, "anchor-rows.json")).anchors;
  const names = Object.keys(anchors).sort();
  const probes = [];
  const stride = Math.max(1, Math.floor(corpus.length / 4));
  for (let k = 0; k < 3; k += 1) {
    const it = corpus[(seed + k * stride) % corpus.length];
    const got = boardRow(engine.api, it.payload);
    const want = it.expected || {};
    const same = got.score === want.score && got.work === want.work
                 && got.metric === want.metric;
    probes.push({ kind: "board", id: it.id, same,
                  got: { score: got.score, work: got.work, metric: got.metric },
                  want: { score: want.score, work: want.work, metric: want.metric },
                  seconds: got.seconds });
    console.log("  probe board " + it.id.padEnd(24) + (same ? " same" : " DRIFT") + " (" + got.seconds + "s)");
  }
  // A random pick, but only among the two cheapest rows: the three rivens
  // differ ~3x in cost and the probe has to answer quickly or the refresh it
  // gates is pointless.  `searched` is the frozen search size, taken from the
  // request and not from any number this probe is checking.
  const pool = [...rivens]
    .sort((a, b) => ((a.expected?.result?.searched) || 1e9) - ((b.expected?.result?.searched) || 1e9))
    .slice(0, Math.min(2, rivens.length));
  const rv = pool[seed % pool.length];
  const gotR = rivenRow(engine.ctx, rv.payload.request, rv.lanes || 6);
  const wantR = (rv.expected || {}).result || {};
  // The wasm returns the optimizer report; the door-level `work` never leaves
  // the native door, so the probe compares the fields both sides really have.
  const sameR = gotR.final_work === wantR.final_work
                && JSON.stringify(gotR.results) === JSON.stringify(wantR.results);
  probes.push({ kind: "riven", id: rv.id, same: sameR,
                got: { work: gotR.work, final_work: gotR.final_work },
                want: { work: wantR.work, final_work: wantR.final_work },
                seconds: gotR.seconds });
  console.log("  probe riven " + rv.id.padEnd(24) + (sameR ? " same" : " DRIFT") + " (" + gotR.seconds + "s)");
  const an = names[seed % names.length];
  const row = anchors[an];
  const gotA = boardRow(engine.api, { record: row.record, ruler: row.ruler, mode: row.mode });
  const sameA = gotA.score === row.score && gotA.work === row.work;
  probes.push({ kind: "anchor", id: an, same: sameA,
                got: { score: gotA.score, work: gotA.work },
                want: { score: row.score, work: row.work }, seconds: gotA.seconds });
  console.log("  probe anchor " + an.padEnd(24) + (sameA ? " same" : " DRIFT") + " (" + gotA.seconds + "s)");

  const drift = probes.filter((p) => !p.same).map((p) => p.id);
  writeOut("probe.json", { ...stamp, mode: "probe", drift: drift.length > 0, drifting: drift, probes });
  console.log(drift.length ? "DRIFT: %s" : "OK: the frozen baseline still matches the wasm", drift.join(", "));
} else {
  console.error("usage: --probe | --board | --riven | --anchors [--file f] [--shard i/n] [--out f] [--site dir]");
  process.exit(2);
}
