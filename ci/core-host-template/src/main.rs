// SPDX-License-Identifier: AGPL-3.0-or-later
//! The smallest HTTP host the TUI needs.
//!
//! The official `wfsim-web` serves the whole browser application. This host
//! serves only the JSON endpoints the portable client calls: the pure routes
//! from `wfsim-webapi` and the stateful optimize job endpoints. The arithmetic
//! is still `wfsim-webapi` + `wfsim-engine`; there is no second implementation.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex, OnceLock};

use serde_json::{json, Value};
use wfsim_optimizer::{set_worker_threads, FunnelState};
use wfsim_webapi::{err_json, funnel_status_json, parse_optimize, run_optimize};

/// THE CLIENT'S WORKER BUDGET, AS IT LAST SET IT.
///
/// `set_worker_threads` is global to the optimizer and the only writer is
/// `POST /api/optimize/threads`.  [`optimize_drive`] runs `threads` lanes of
/// its own, so each lane must be single-threaded or the two budgets multiply
/// (lanes × workers over the same cores).  This mirror is how the drive puts
/// the budget back afterwards without asking the optimizer for a value it does
/// not publish.  Zero means "the client never set one", and then the drive
/// restores the platform's own count.
static WORKER_THREADS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// The budget to restore: what the client set, else one per usable core.
fn client_worker_threads() -> usize {
    match WORKER_THREADS.load(Ordering::Relaxed) {
        0 => std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1),
        n => n,
    }
}

/// Makes the optimizer single-threaded for as long as it lives, then restores
/// the client's budget.  A guard rather than a call at the end because
/// [`optimize_drive`] returns early from six places, and a seventh added later
/// would otherwise leave every later request single-threaded.
struct WorkerBudget;

impl WorkerBudget {
    fn single() -> Self {
        set_worker_threads(1);
        Self
    }
}

impl Drop for WorkerBudget {
    fn drop(&mut self) {
        set_worker_threads(client_worker_threads());
    }
}

/// ALLOCATION METERING, OFF UNLESS `WFSIM_MEM_TRACE=1`.
///
/// The engine's own counters say how much ARITHMETIC a row costs and say
/// nothing about how often it goes to the allocator.  A counting `GlobalAlloc`
/// wrapping `System` answers that.  The switch is read once and the counters
/// are relaxed adds, so with the flag down an allocation costs one load and a
/// branch — which is why this can stay in a release build.
mod memtrace {
    // FAST ALLOCATOR, OPT-IN.  `fast-alloc` is a `wfsim-core` feature that
    // `sync_engine` turns on when it found a MinGW gcc for cc-rs (mimalloc is C
    // source).  Measured 2026-10-16, bit for bit identical: +6.3% on board
    // rows, +6.1% on riven searches.  With the feature off the inner allocator
    // is `System`, i.e. exactly upstream's behaviour.
    #[cfg(feature = "fast-alloc")]
    use mimalloc::MiMalloc;
    use std::alloc::{GlobalAlloc, Layout};
    // A type alias cannot be used as a value (`INNER.alloc(l)` is E0423), so the
    // inner allocator is a const of whichever unit struct is selected.
    #[cfg(feature = "fast-alloc")]
    const INNER: MiMalloc = MiMalloc;
    #[cfg(not(feature = "fast-alloc"))]
    const INNER: std::alloc::System = std::alloc::System;
    use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
    use std::sync::OnceLock;

    static ON: AtomicBool = AtomicBool::new(false);
    static ALLOCS: AtomicU64 = AtomicU64::new(0);
    static ALLOC_BYTES: AtomicU64 = AtomicU64::new(0);
    static DEALLOCS: AtomicU64 = AtomicU64::new(0);
    static LIVE_BYTES: AtomicI64 = AtomicI64::new(0);
    static PEAK_LIVE: AtomicI64 = AtomicI64::new(0);

    pub fn init() {
        static ONCE: OnceLock<()> = OnceLock::new();
        ONCE.get_or_init(|| {
            if std::env::var_os("WFSIM_MEM_TRACE").is_some() {
                ON.store(true, Ordering::Relaxed);
            }
        });
    }

    #[inline(always)]
    fn on() -> bool {
        ON.load(Ordering::Relaxed)
    }

    /// Per-row, so `peak_live` describes THIS row and not the process's life.
    pub fn reset() {
        ALLOCS.store(0, Ordering::Relaxed);
        ALLOC_BYTES.store(0, Ordering::Relaxed);
        DEALLOCS.store(0, Ordering::Relaxed);
        LIVE_BYTES.store(0, Ordering::Relaxed);
        PEAK_LIVE.store(0, Ordering::Relaxed);
    }

    pub fn snapshot() -> (u64, u64, u64, i64) {
        (ALLOCS.load(Ordering::Relaxed), ALLOC_BYTES.load(Ordering::Relaxed),
         DEALLOCS.load(Ordering::Relaxed), PEAK_LIVE.load(Ordering::Relaxed))
    }

    pub struct Counting;

    unsafe impl GlobalAlloc for Counting {
        unsafe fn alloc(&self, l: Layout) -> *mut u8 {
            let p = INNER.alloc(l);
            if !p.is_null() && on() {
                ALLOCS.fetch_add(1, Ordering::Relaxed);
                ALLOC_BYTES.fetch_add(l.size() as u64, Ordering::Relaxed);
                let live = LIVE_BYTES.fetch_add(l.size() as i64, Ordering::Relaxed)
                    + l.size() as i64;
                PEAK_LIVE.fetch_max(live, Ordering::Relaxed);
            }
            p
        }
        unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
            if on() {
                DEALLOCS.fetch_add(1, Ordering::Relaxed);
                LIVE_BYTES.fetch_sub(l.size() as i64, Ordering::Relaxed);
            }
            INNER.dealloc(p, l)
        }
        unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
            let q = INNER.realloc(p, l, new);
            if !q.is_null() && on() {
                ALLOCS.fetch_add(1, Ordering::Relaxed);
                ALLOC_BYTES.fetch_add(new as u64, Ordering::Relaxed);
                let d = new as i64 - l.size() as i64;
                let live = LIVE_BYTES.fetch_add(d, Ordering::Relaxed) + d;
                PEAK_LIVE.fetch_max(live, Ordering::Relaxed);
            }
            q
        }
    }
}

#[global_allocator]
static GLOBAL: memtrace::Counting = memtrace::Counting;

struct Request {
    method: String,
    path: String,
    body: Vec<u8>,
}

fn read_request(stream: &TcpStream) -> std::io::Result<Option<Request>> {
    let mut reader = BufReader::new(stream);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line)? == 0 {
        return Ok(None);
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("/").to_string();
    let mut content_length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        if line == "\r\n" || line == "\n" {
            break;
        }
        if let Some(v) = line
            .to_ascii_lowercase()
            .strip_prefix("content-length:")
            .map(|s| s.trim().to_string())
        {
            content_length = v.parse().unwrap_or(0);
        }
    }
    let mut body = vec![0u8; content_length];
    if content_length > 0 {
        reader.read_exact(&mut body)?;
    }
    Ok(Some(Request { method, path, body }))
}

fn respond(
    stream: &mut TcpStream,
    status: &str,
    content_type: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let header = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n",
        body.len()
    );
    stream.write_all(header.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()
}

fn respond_json(stream: &mut TcpStream, value: &Value) -> std::io::Result<()> {
    respond(
        stream,
        "200 OK",
        "application/json; charset=utf-8",
        value.to_string().as_bytes(),
    )
}

struct OptJob {
    id: u64,
    started: std::time::Instant,
    state: Arc<FunnelState>,
    phase: Mutex<&'static str>,
    counts: Mutex<Option<(usize, usize)>>,
    result: Mutex<Option<Value>>,
}

impl OptJob {
    fn active(&self) -> bool {
        matches!(*self.phase.lock().unwrap(), "enumerating" | "running")
    }
}

fn opt_jobs() -> &'static Mutex<Vec<Arc<OptJob>>> {
    static J: OnceLock<Mutex<Vec<Arc<OptJob>>>> = OnceLock::new();
    J.get_or_init(|| Mutex::new(Vec::new()))
}

fn opt_job(v: &Value) -> Option<Arc<OptJob>> {
    let jobs = opt_jobs().lock().unwrap();
    match v.get("id").and_then(Value::as_u64) {
        Some(id) => jobs.iter().find(|j| j.id == id).cloned(),
        None => jobs.last().cloned(),
    }
}

fn optimize_status(v: &Value) -> Value {
    let Some(j) = opt_job(v) else {
        return err_json("no such optimize job");
    };
    let phase = *j.phase.lock().unwrap();
    let counts = *j.counts.lock().unwrap();
    let mut out = funnel_status_json(&j.state, phase, counts, j.started.elapsed().as_secs_f64());
    out["job_id"] = json!(j.id);
    if let Some(r) = j.result.lock().unwrap().clone() {
        out["result"] = r;
    }
    out
}

fn optimize_cancel(v: &Value) -> Value {
    let Some(j) = opt_job(v) else {
        return err_json("no such optimize job");
    };
    j.state.cancel.store(true, Ordering::Relaxed);
    json!({ "ok": true, "job_id": j.id })
}

/// THE PAGE'S OWN LOOP, IN THIS PROCESS — `quickFleet`
/// (08-checkpoint-api.js:144), with function calls where the page uses worker
/// round trips.  A quick request that names no `quick_fleet` of its own is
/// driven here: the leader's rounds all run on THIS thread (the fleet's shared
/// state is thread-local, `optimize/quick.rs` `FLEET`) while the scorers'
/// slices run in parallel beside it, and the number reported is the page's own
/// — the sum of the scorers' work plus the last round's `final_work`.
///
/// WHY THIS AND NOT THE ONE-SHOT PATH: the fact factory compares `work` as part
/// of a riven answer's identity, so the number has to be the one every other
/// volunteer's client produces.  The one-shot quick path counts the same search
/// its own way and disagrees with the page by a fraction of a percent on some
/// searches — enough that the answer can never match another client's.
fn optimize_drive(v: &Value, state: &Arc<FunnelState>,
                  progress: &dyn Fn(usize, usize)) -> Value {
    let threads = v.get("threads").and_then(Value::as_u64).unwrap_or(8).clamp(1, 64) as usize;
    // ONE PARSE FOR THE WHOLE SEARCH (fleet patch 009).  Every round used to
    // re-parse the request — candidate pool, forma resolution and all — and so
    // did every lane: `1 + lanes` parses a round.  The plan is the same plan
    // every time; only its `quick_fleet` message changes, and `for_fleet`
    // swaps exactly that.
    let mut body = v.clone();
    body["quick_fleet"] = json!({ "lead": true, "fresh": true, "scores": Vec::<Value>::new() });
    let plan0 = match parse_optimize(&body) {
        Ok(p) => p,
        Err(e) => return e,
    };
    // THE LANES ARE SINGLE-THREADED (fleet patch 007).  The page runs one
    // leader and N browser workers, and a worker IS one thread.  Here the lanes
    // share a process, so a lane that also lets the optimizer fan out multiplies
    // the two budgets.  The guard restores the client's budget on every exit.
    let _budget = WorkerBudget::single();
    let mut scores: Vec<Value> = Vec::new();
    let mut worked: u64 = 0;
    let mut built: u64 = 0;
    let mut fought: u64 = 0;
    let mut step = 0usize;
    loop {
        let out = run_optimize(
            plan0.for_fleet(json!({ "lead": true, "fresh": step == 0, "scores": scores })),
            state,
            |c: usize, j: usize| progress(c, j),
            None,
        );
        if out.get("ok").and_then(Value::as_bool) != Some(true) {
            return out;
        }
        let pending = match out.get("pending").and_then(Value::as_array).cloned() {
            Some(p) if !p.is_empty() => p,
            // NO PENDING: the search is over.  `work` is the scorers' total plus
            // the final round, which is what the page sends.
            _ => {
                let mut done = out;
                done["search_work"] = json!(worked);
                done["work"] = json!(worked
                    + done.get("final_work").and_then(Value::as_u64).unwrap_or(0));
                done["builds"] = json!(built);
                done["fights"] = json!(fought);
                return done;
            }
        };
        // TWO SLICES A WORKER, the page's own split: enough that a fast worker
        // takes a slow one's share, few enough that the fixed cost stays small.
        let k = threads.min(pending.len()).max(1);
        let size = pending.len().div_ceil(k * 2).max(1);
        let slices: Vec<Vec<Value>> = pending.chunks(size).map(|c| c.to_vec()).collect();
        let next = std::sync::atomic::AtomicUsize::new(0);
        let got: Mutex<Vec<Option<Value>>> = Mutex::new((0..slices.len()).map(|_| None).collect());
        let failed: Mutex<Option<Value>> = Mutex::new(None);
        std::thread::scope(|scope| {
            for _ in 0..k.min(slices.len()) {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= slices.len() {
                        break;
                    }
                    let one = run_optimize(
                        plan0.for_fleet(json!({ "score": slices[i] })),
                        state,
                        |_c: usize, _j: usize| {},
                        None,
                    );
                    if one.get("ok").and_then(Value::as_bool) != Some(true) {
                        *failed.lock().unwrap() = Some(one);
                        break;
                    }
                    got.lock().unwrap()[i] = Some(one);
                });
            }
        });
        if let Some(e) = failed.lock().unwrap().take() {
            return e;
        }
        let mut round: Vec<Value> = Vec::new();
        for slot in got.lock().unwrap().iter() {
            match slot {
                Some(one) => {
                    worked = worked
                        .saturating_add(one.get("work").and_then(Value::as_u64).unwrap_or(0));
                    fought = fought
                        .saturating_add(one.get("fights").and_then(Value::as_u64).unwrap_or(0));
                    round.extend(one.get("scores").and_then(Value::as_array).cloned()
                        .unwrap_or_default());
                }
                None => return err_json("a scoring slice never ran"),
            }
        }
        built += round.len() as u64;
        scores = round;
        step += 1;
    }
}

/// ONE BOARD ROW, ORDERED, FOUGHT, FOLDED AND SCORED HERE — the four calls a
/// client makes, as one.  The arithmetic is the same `wfsim_webapi` functions
/// (`/api/board/{order,runs,fold,score}`), so the answer is the one those four
/// give; one call is what makes a row cost a client one round trip.
///
/// THE RUNS ARE FOLDED IN PLACE.  `runs` then `fold({pieces})` would walk the
/// same thousand shards through `serde_json::Value` twice — once to hand each
/// one out, once to read it back — and that round trip is SINGLE-THREADED: the
/// fight's lanes sit idle while one thread serializes and re-parses a row's
/// worth of shards.  Measured at about a fifth of a row's wall time.  `fold`
/// with `from`/`count` fights the runs on the same lanes and folds them in run
/// order (`fight::shards_parallel` + `Shard::merge`), which is exactly what the
/// two-call path did, so `score`, `work` and `counts` are identical to the bit
/// — only the JSON in the middle is gone.  `board_runs_json` and
/// `board_fold_json` are untouched, so the page's own four calls still work.
/// A BATCH OF ROWS, WITH THE ORDER-PARSE OF ROW k OVERLAPPING THE FOLD OF k-1.
///
/// WHY (2026-10-16): a row has a serial prologue and epilogue (`order` measured
/// 26-88 ms, plus `score`), which is 0.6-2.1% of a 1-3 s row — exactly the gap
/// between our 97.5% lane occupancy and the 99%+ that a project with an order
/// of magnitude bigger tasks (simfleet's riven searches: 44-163 s) gets for
/// free.  Parse row k+1 while row k folds and the prologue stops being dead
/// time for the lanes.
///
/// FOLDS STAY STRICTLY SERIAL.  Two rows folding at once would have both asking
/// the lane pool for every thread, which is the oversubscription we already
/// measured to cost occupancy — so one fold is spawned at a time and joined on
/// the next iteration (after that iteration's parse, which is the overlap).
fn score_row(folded: Option<Value>, request: Value, ruler: Value) -> Value {
    let folded = match folded {
        Some(f) => f,
        None => return err_json("fold thread failed"),
    };
    if folded.get("ok").and_then(Value::as_bool) != Some(true) {
        return stepped(folded, "fold");
    }
    match wfsim_webapi::route("/api/board/score", &json!({
        "request": request, "ruler": ruler,
        "acc": folded.get("acc").cloned().unwrap_or(Value::Null) })) {
        Some(s) if s.get("ok").and_then(Value::as_bool) == Some(true) => s,
        Some(s) => stepped(s, "score"),
        None => err_json("no board/score route"),
    }
}

/// A BATCH OF ROWS, PIPELINED SO THE LANES NEVER WAIT ON A SERIAL STAGE.
///
/// WHAT THE PHASE TRACE SAYS (2026-10-16, steady rows): `order` is only 1-6 ms
/// but `score` is 6-20 ms, on rows of 0.47-2.44 s — i.e. the serial epilogue is
/// 1.2-4.3% of a row, and the lanes sit idle through all of it.  (An earlier
/// version of this overlapped `order` instead and measured B/S = 1.001: the
/// serial stage that matters is `score`, not `order`.)
///
/// THE PIPELINE, one row per iteration:
///   1. collect the score of row k-2 (it ran during fold[k-1]);
///   2. parse order[k]                       — while fold[k-1] still runs;
///   3. join fold[k-1], hand row k-1's score to a side thread;
///   4. start fold[k]                        — lanes never idle between folds.
/// FOLDS ARE STRICTLY SERIAL: exactly one fold is ever in flight, so the lane
/// pool is never asked for its threads twice (that oversubscription is what
/// costs occupancy when two rows fold at once).
fn board_rows_batch(v: &Value) -> Value {
    let rows = match v.get("rows").and_then(Value::as_array) {
        Some(r) if !r.is_empty() => r.clone(),
        _ => return err_json("no rows"),
    };
    let threads = v.get("threads").and_then(Value::as_u64).unwrap_or(1).clamp(1, 256);
    let mut out: Vec<Value> = vec![Value::Null; rows.len()];
    let mut fold_pending: Option<(std::thread::JoinHandle<Option<Value>>, Value, Value, usize)> = None;
    let mut score_pending: Option<(std::thread::JoinHandle<Value>, usize)> = None;
    for (k, row) in rows.iter().enumerate() {
        // 1. the score of row k-2, which overlapped fold[k-1].
        if let Some((h, i)) = score_pending.take() {
            out[i] = h.join().unwrap_or_else(|_| err_json("score thread failed"));
        }
        // 2. this row's order, parsed while the previous fold is still running.
        let mut rowv = row.clone();
        if let Some(o) = rowv.as_object_mut() {
            o.insert("threads".to_string(), json!(threads));
        }
        let order = match wfsim_webapi::route("/api/board/order", &rowv) {
            Some(o) => o,
            None => { out[k] = err_json("no board/order route"); continue; }
        };
        if order.get("ok").and_then(Value::as_bool) != Some(true) {
            out[k] = stepped(order, "order");
            continue;
        }
        // 3. closer of fold[k-1]: score it on a SIDE thread so it overlaps fold[k].
        if let Some((h, req, rv, i)) = fold_pending.take() {
            let folded = h.join().ok().flatten();
            score_pending = Some((std::thread::spawn(move || score_row(folded, req, rv)), i));
        }
        // 4. start fold[k] now; the lanes pick it up immediately.
        let req = order.get("request").cloned().unwrap_or(Value::Null);
        let runs = req.get("runs").and_then(Value::as_u64).unwrap_or(0);
        let req2 = req.clone();
        let h = std::thread::spawn(move || {
            wfsim_webapi::route("/api/board/fold", &json!({
                "request": req2, "acc": Value::Null, "from": 0, "count": runs,
                "threads": threads }))
        });
        fold_pending = Some((h, req, rowv.get("ruler").cloned().unwrap_or(Value::Null), k));
    }
    if let Some((h, req, rv, i)) = fold_pending.take() {
        let folded = h.join().ok().flatten();
        out[i] = score_row(folded, req, rv);
    }
    if let Some((h, i)) = score_pending.take() {
        out[i] = h.join().unwrap_or_else(|_| err_json("score thread failed"));
    }
    json!({ "ok": true, "results": out })
}

fn board_row_once(v: &Value) -> Value {
    // PHASE TRACE — off unless `WFSIM_PHASE_TRACE=1`, and then one stderr line
    // per row naming how long each stage took and how much CPU the process
    // burned through it.  It is how a reader sees WHERE a row's wall time and
    // CPU time go (order is serial, runs is the parallel fight, fold is the
    // serial merge, score is serial) without a profiler and without slowing a
    // normal build: the flag is read once per row, not per run.
    let trace = std::env::var_os("WFSIM_PHASE_TRACE").is_some();
    if trace {
        memtrace::init();
        memtrace::reset();
    }
    let cpu0 = if trace { process_cpu_seconds() } else { 0.0 };
    let t_all = std::time::Instant::now();
    let order = match wfsim_webapi::route("/api/board/order", v) {
        Some(o) => o,
        None => return err_json("no board/order route"),
    };
    if order.get("ok").and_then(Value::as_bool) != Some(true) {
        return stepped(order, "order");
    }
    let t_order = t_all.elapsed().as_secs_f64();
    let request = order.get("request").cloned().unwrap_or(Value::Null);
    let runs = request.get("runs").and_then(Value::as_u64).unwrap_or(0);
    let threads = v.get("threads").and_then(Value::as_u64).unwrap_or(1).clamp(1, 256);
    let t_runs0 = std::time::Instant::now();
    let folded = match wfsim_webapi::route("/api/board/fold", &json!({
        "request": request, "acc": Value::Null,
        "from": 0, "count": runs, "threads": threads })) {
        Some(f) => f,
        None => return err_json("no board/fold route"),
    };
    let t_fold = t_runs0.elapsed().as_secs_f64();
    if folded.get("ok").and_then(Value::as_bool) != Some(true) {
        return stepped(folded, "fold");
    }
    let t_score0 = std::time::Instant::now();
    let out = match wfsim_webapi::route("/api/board/score", &json!({
        "request": request, "ruler": v.get("ruler").cloned().unwrap_or(Value::Null),
        "acc": folded.get("acc").cloned().unwrap_or(Value::Null) })) {
        Some(s) if s.get("ok").and_then(Value::as_bool) == Some(true) => s,
        Some(s) => stepped(s, "score"),
        None => err_json("no board/score route"),
    };
    if trace {
        let t_score = t_score0.elapsed().as_secs_f64();
        let total = t_all.elapsed().as_secs_f64();
        let cpu = (process_cpu_seconds() - cpu0).max(0.0);
        let (allocs, alloc_bytes, deallocs, peak_live) = memtrace::snapshot();
        eprintln!(
            "PHASE row runs={runs} lanes={threads} total={total:.3} order={t_order:.3} \
fold(fight+merge)={t_fold:.3} score={t_score:.3} cpu_s={cpu:.2} cores={:.2} \
allocs={allocs} alloc_kb={} deallocs={deallocs} peak_live_kb={}",
            if total > 0.0 { cpu / total } else { 0.0 },
            alloc_bytes / 1024, peak_live / 1024);
    }
    out
}

/// THIS PROCESS'S CPU SECONDS (kernel + user), or 0.0 when unreadable.  The
/// host is a small program already opening process handles for the console; a
/// Win32 `GetProcessTimes` is enough and needs no crate.
#[cfg(windows)]
fn process_cpu_seconds() -> f64 {
    use std::mem::MaybeUninit;
    #[repr(C)] #[derive(Default, Clone, Copy)]
    struct FileTime { lo: u32, hi: u32 }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> isize;
        fn GetProcessTimes(h: isize, c: *mut FileTime, e: *mut FileTime,
                           k: *mut FileTime, u: *mut FileTime) -> i32;
    }
    unsafe {
        let (mut c, mut e, mut k, mut u) = (MaybeUninit::zeroed(), MaybeUninit::zeroed(),
                                            MaybeUninit::zeroed(), MaybeUninit::zeroed());
        if GetProcessTimes(GetCurrentProcess(), c.as_mut_ptr(), e.as_mut_ptr(),
                           k.as_mut_ptr(), u.as_mut_ptr()) == 0 {
            return 0.0;
        }
        let f = |t: MaybeUninit<FileTime>| { let t = t.assume_init(); (u64::from(t.hi) << 32) | u64::from(t.lo) };
        (f(k) + f(u)) as f64 / 1e7
    }
}

#[cfg(not(windows))]
fn process_cpu_seconds() -> f64 { 0.0 }

/// A REFUSAL SAYS WHICH STEP MADE IT.  The order's refusal is the row's own
/// (`validate_for_board`: a build no fight can run) and the client skips that
/// row; one from runs/fold/score is this process failing and the client keeps
/// the row.
fn stepped(why: Value, step: &str) -> Value {
    match why {
        Value::Object(mut m) => {
            m.insert("step".into(), json!(step));
            Value::Object(m)
        }
        other => json!({ "ok": false, "step": step, "error": other.to_string() }),
    }
}

fn optimize_start(v: &Value) -> Value {
    static NEXT_JOB_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let sync = v.get("sync").and_then(Value::as_bool).unwrap_or(false);
    {
        let jobs = opt_jobs().lock().unwrap();
        if let Some(j) = jobs.iter().find(|j| j.active()) {
            return json!({
                "ok": false,
                "error": "an optimization is already running - cancel it or wait",
                "job_id": j.id,
            });
        }
    }
    // A QUICK SEARCH IS DRIVEN HERE, the page's own way (see `optimize_drive`).
    // The one-shot path stays for a funnel, a resume, and a client that already
    // speaks the fleet protocol itself.
    let driven = v.get("quick_fleet").is_none()
        && v.get("strategy").and_then(Value::as_str) == Some("quick");
    let plan = if driven {
        None
    } else {
        match parse_optimize(v) {
            Ok(p) => Some(p),
            Err(e) => return e,
        }
    };
    let id = NEXT_JOB_ID.fetch_add(1, Ordering::Relaxed);
    let job = Arc::new(OptJob {
        id,
        started: std::time::Instant::now(),
        state: Arc::new(FunnelState::default()),
        phase: Mutex::new("running"),
        counts: Mutex::new(None),
        result: Mutex::new(None),
    });
    {
        let mut jobs = opt_jobs().lock().unwrap();
        jobs.push(job.clone());
        while jobs.len() > 6 {
            match jobs.iter().position(|j| !j.active()) {
                Some(pos) => drop(jobs.remove(pos)),
                None => break,
            }
        }
    }
    if sync {
        wfsim_optimizer::deprioritize_current_thread();
        let state = job.state.clone();
        let result = match plan {
            Some(plan) => run_optimize(plan, &state, |_c: usize, _j: usize| {}, None),
            None => optimize_drive(v, &state, &|_c: usize, _j: usize| {}),
        };
        *job.result.lock().unwrap() = Some(result.clone());
        *job.phase.lock().unwrap() = "done";
        return result;
    }
    let worker = job.clone();
    let request = v.clone();
    std::thread::spawn(move || {
        wfsim_optimizer::deprioritize_current_thread();
        let result = match plan {
            Some(plan) => run_optimize(
                plan,
                &worker.state,
                |cands, jobs| {
                    *worker.counts.lock().unwrap() = Some((cands, jobs));
                    *worker.phase.lock().unwrap() = "running";
                },
                None,
            ),
            None => optimize_drive(&request, &worker.state, &|cands: usize, jobs: usize| {
                *worker.counts.lock().unwrap() = Some((cands, jobs));
                *worker.phase.lock().unwrap() = "running";
            }),
        };
        let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
        let cancelled = result
            .get("cancelled")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        *worker.result.lock().unwrap() = Some(result);
        *worker.phase.lock().unwrap() = if !ok {
            "error"
        } else if cancelled {
            "cancelled"
        } else {
            "done"
        };
    });
    json!({ "ok": true, "job_id": id })
}

fn handle(mut stream: TcpStream) -> std::io::Result<()> {
    let Some(req) = read_request(&stream)? else {
        return Ok(());
    };
    let path = req.path.split('?').next().unwrap_or("/");
    let body = serde_json::from_slice::<Value>(&req.body).unwrap_or(Value::Null);
    match (req.method.as_str(), path) {
        ("POST", "/api/optimize") => respond_json(&mut stream, &optimize_start(&body)),
        ("POST", "/api/board/row") => respond_json(&mut stream, &board_row_once(&body)),
        ("POST", "/api/board/rows") => respond_json(&mut stream, &board_rows_batch(&body)),
        // DIAGNOSTIC ONLY, for PGO profile collection (2026-10-16): LLVM writes
        // its profile at process EXIT, and every engine this client runs is
        // `terminate()`d — which leaves `truncated profile data` and makes
        // `llvm-profdata merge` refuse the file.  This is the one clean way to
        // stop an engine, so a profiled build can be measured at all.  It is
        // NOT used by the shipping client: nothing calls it, and the flag that
        // makes it matter (`-Cprofile-generate`) only exists in bench builds.
        ("POST", "/api/quit") => {
            // DUMP BEFORE EXITING.  On this target the LLVM profile runtime's
            // atexit hook never runs (normal mode wrote nothing even on a clean
            // xit(0)), and continuous mode leaves a file llvm-profdata calls
            // 	runcated.  Calling the runtime's own dump entry point is what
            // produces a complete, mergeable profile.
            // NOTE: a profiled build may call __llvm_profile_write_file()
            // here to dump before exiting (that symbol only exists when the
            // profiler runtime is linked, i.e. under -Cprofile-generate).
            // 2.5 PGO TRAINING: `-Cprofile-generate` dumps its counters at
            // process exit, and this target's runtime never gets there (see the
            // notes above), so the dump is called explicitly here.  The symbol
            // exists ONLY in an instrumented build, hence the cfg: a
            // `profile-use` build has neither the feature nor the symbol.
            #[cfg(feature = "pgo-exit")]
            unsafe {
                extern "C" {
                    fn __llvm_profile_write_file() -> i32;
                }
                __llvm_profile_write_file();
            }
            let _ = respond_json(&mut stream, &json!({ "ok": true, "quitting": true }));
            std::process::exit(0);
        }
        ("POST", "/api/optimize/status") => respond_json(&mut stream, &optimize_status(&body)),
        ("POST", "/api/optimize/cancel") => respond_json(&mut stream, &optimize_cancel(&body)),
        ("POST", "/api/optimize/threads") => {
            let n = body.get("threads").and_then(Value::as_u64).unwrap_or(0);
            if n == 0 {
                respond_json(&mut stream, &json!({ "ok": false, "error": "threads must be > 0" }))
            } else {
                WORKER_THREADS.store(n as usize, Ordering::Relaxed);
                set_worker_threads(n as usize);
                respond_json(&mut stream, &json!({ "ok": true, "threads": n }))
            }
        }
        _ => match wfsim_webapi::route(path, &body) {
            Some(out) => respond_json(&mut stream, &out),
            None => respond(
                &mut stream,
                "404 Not Found",
                "text/plain; charset=utf-8",
                b"not found",
            ),
        },
    }
}

fn main() {
    let port = std::env::var("WFSIM_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8787);
    let listener = TcpListener::bind(("127.0.0.1", port)).expect("bind WFSIM_PORT");
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                std::thread::spawn(move || {
                    let _ = handle(stream);
                });
            }
            Err(e) => eprintln!("accept: {e}"),
        }
    }
}
