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
    let plan = match parse_optimize(v) {
        Ok(p) => p,
        Err(e) => return e,
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
        let result = run_optimize(plan, &job.state, |_c: usize, _j: usize| {}, None);
        *job.result.lock().unwrap() = Some(result.clone());
        *job.phase.lock().unwrap() = "done";
        return result;
    }
    let worker = job.clone();
    std::thread::spawn(move || {
        wfsim_optimizer::deprioritize_current_thread();
        let result = run_optimize(
            plan,
            &worker.state,
            |cands, jobs| {
                *worker.counts.lock().unwrap() = Some((cands, jobs));
                *worker.phase.lock().unwrap() = "running";
            },
            None,
        );
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
        ("POST", "/api/optimize/status") => respond_json(&mut stream, &optimize_status(&body)),
        ("POST", "/api/optimize/cancel") => respond_json(&mut stream, &optimize_cancel(&body)),
        ("POST", "/api/optimize/threads") => {
            let n = body.get("threads").and_then(Value::as_u64).unwrap_or(0);
            if n == 0 {
                respond_json(&mut stream, &json!({ "ok": false, "error": "threads must be > 0" }))
            } else {
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
