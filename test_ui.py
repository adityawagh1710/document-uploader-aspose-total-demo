"""
Office Convert Test UI — Monitoring + Conversion Dashboard.

- Stats refresh every 2s at the top — stay live during conversion (background thread)
- Conversion runs in a background thread; main script stays responsive
- Active conversion and recent results are process-wide, so a browser refresh
  reattaches to the running conversion and surfaces its completion
- Conversion history with time taken + download buttons
- Error display

Run: docker compose up -d
Open: http://localhost:8501
"""

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

API_URL = os.environ.get("API_URL", "http://localhost:8080")
CONVERT_URL = f"{API_URL}/convert"
HEALTH_URL = f"{API_URL}/health"
HEARTBEATS_URL = f"{API_URL}/jobs"  # /jobs/{request_id}/heartbeats
PROGRESS_URL = f"{API_URL}/jobs"  # /jobs/{request_id}/progress

MAX_RECENT_RESULTS = 20
ERROR_DISPLAY_WINDOW_SEC = 60

st.set_page_config(page_title="Office Convert Dashboard", layout="wide")


# ============================================================
# Process-wide state (survives browser refresh / second tabs / reruns;
# wiped on Streamlit process restart, which is fine because the
# backend HTTP connection dies with it too).
#
# Wrapped in @st.cache_resource because Streamlit re-executes the script
# top-to-bottom on every rerun — plain module-level vars would reset.
# ============================================================
@st.cache_resource
def _state() -> dict:
    return {
        "lock": threading.Lock(),
        "active": None,  # {id, holder, thread, start_time, input_name, input_size_mb}
        "results": [],  # successful results, newest first
        "last_error": None,  # {"msg": str, "ts": float}
    }


def get_health():
    try:
        return requests.get(HEALTH_URL, timeout=3).json()
    except Exception as e:
        return {"error": str(e)}


_DEFAULT_DOCKER_STATS = {"cpu": "N/A", "mem_usage": "N/A", "mem_pct": "N/A", "pids": "N/A"}


@st.cache_resource
def _docker_monitor() -> dict:
    """Background-thread cache of `docker stats` + `docker top`.

    `docker stats --no-stream` takes ~1–2s per call. If we ran it inside the
    fragment, every tick would block for that long and updates would arrive in
    big bunches, amplifying visible flicker. Instead a daemon thread refreshes
    the cache on its own cadence; the fragment just reads the dict — O(µs).
    """
    state: dict = {
        "lock": threading.Lock(),
        "stats": dict(_DEFAULT_DOCKER_STATS),
        "workers": [],
    }

    def _refresh_loop() -> None:
        while True:
            new_stats = dict(_DEFAULT_DOCKER_STATS)
            try:
                result = subprocess.run(
                    [
                        "docker",
                        "stats",
                        "office-convert",
                        "--no-stream",
                        "--format",
                        "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts = result.stdout.strip().split("\t")
                    new_stats = {
                        "cpu": parts[0],
                        "mem_usage": parts[1],
                        "mem_pct": parts[2],
                        "pids": parts[3],
                    }
            except Exception:
                pass

            new_workers: list[str] = []
            try:
                result = subprocess.run(
                    ["docker", "top", "office-convert", "-o", "pid,pcpu,pmem,time,args"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    new_workers = [
                        line
                        for line in result.stdout.strip().split("\n")
                        if "office-convert-worker" in line
                    ]
            except Exception:
                pass

            with state["lock"]:
                state["stats"] = new_stats
                state["workers"] = new_workers

            time.sleep(1.5)  # docker stats itself takes ~1.5s, so this is "as fast as possible"

    thread = threading.Thread(target=_refresh_loop, daemon=True)
    thread.start()
    return state


def get_docker_stats() -> dict:
    m = _docker_monitor()
    with m["lock"]:
        return dict(m["stats"])


def get_worker_processes() -> list[str]:
    m = _docker_monitor()
    with m["lock"]:
        return list(m["workers"])


def get_heartbeats(request_id: str) -> list[dict]:
    """Fetch the heartbeat trail for an in-flight (or recently-completed) request."""
    try:
        resp = requests.get(f"{HEARTBEATS_URL}/{request_id}/heartbeats", timeout=2)
        if resp.status_code != 200:
            return []
        return resp.json().get("heartbeats", []) or []
    except Exception:
        return []


def get_progress(request_id: str) -> dict:
    """Fetch weighted progress for an in-flight request."""
    try:
        resp = requests.get(f"{PROGRESS_URL}/{request_id}/progress", timeout=2)
        if resp.status_code != 200:
            return {}
        return resp.json() or {}
    except Exception:
        return {}


def do_conversion(file_name, file_bytes, request_id):
    """Blocking conversion. Returns (data, elapsed, error)."""
    start = time.time()
    try:
        resp = requests.post(
            CONVERT_URL,
            files={"file": (file_name, file_bytes)},
            headers={"X-Request-ID": request_id},
            stream=True,
            timeout=1800,
        )
        if resp.status_code != 200:
            try:
                body = resp.json()
                return (
                    None,
                    time.time() - start,
                    f"Error {resp.status_code}: {body.get('failure_class', 'unknown')}",
                )
            except Exception:
                return None, time.time() - start, f"Error {resp.status_code}"
        chunks = []
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
        data = b"".join(chunks)
        elapsed = time.time() - start
        if not data or not data.startswith(b"%PDF"):
            return None, elapsed, "Invalid output (not a PDF)"
        return data, elapsed, None
    except Exception as e:
        return None, time.time() - start, str(e)


def _conversion_worker(file_name, file_bytes, request_id, holder):
    """Runs in a background thread. Writes result into shared holder dict."""
    data, elapsed, error = do_conversion(file_name, file_bytes, request_id)
    holder["data"] = data
    holder["elapsed"] = elapsed
    holder["error"] = error
    holder["done"] = True


def _start_conversion(file_name: str, file_bytes: bytes) -> bool:
    """Atomically start a process-wide conversion. Returns False if one is already running."""
    s = _state()
    with s["lock"]:
        if s["active"] is not None:
            return False
        # The same UUID is the conversion record ID, the backend's X-Request-ID,
        # and the key for polling /jobs/{id}/heartbeats — keeping all three in
        # sync means the UI's heartbeat panel correlates 1:1 with the in-flight
        # request on the server.
        request_id = uuid.uuid4().hex
        holder = {"done": False}
        thread = threading.Thread(
            target=_conversion_worker,
            args=(file_name, file_bytes, request_id, holder),
            daemon=True,
        )
        add_script_run_ctx(thread)
        s["active"] = {
            "id": request_id,
            "holder": holder,
            "thread": thread,
            "start_time": time.time(),
            "input_name": file_name,
            "input_size_mb": len(file_bytes) / 1024 / 1024,
        }
        thread.start()
        return True


def _collect_if_finished() -> bool:
    """If the active conversion is done, move it to results/error and clear active. Returns True if something changed."""
    s = _state()
    with s["lock"]:
        if s["active"] is None:
            return False
        holder = s["active"]["holder"]
        if not holder.get("done"):
            return False
        input_name = s["active"]["input_name"]
        input_size_mb = s["active"]["input_size_mb"]
        result_id = s["active"]["id"]
        error = holder.get("error")
        elapsed = holder.get("elapsed", 0.0)

        if error:
            s["last_error"] = {
                "msg": f"❌ {error} on {input_name} (after {elapsed:.1f}s)",
                "ts": time.time(),
            }
        else:
            data = holder.get("data")
            out_name = Path(input_name).stem + ".pdf"
            s["results"].insert(
                0,
                {
                    "id": result_id,
                    "name": out_name,
                    "input": input_name,
                    "data": data,
                    "out_mb": len(data) / 1024 / 1024,
                    "in_mb": input_size_mb,
                    "time": elapsed,
                    "ts": time.strftime("%H:%M:%S"),
                },
            )
            del s["results"][MAX_RECENT_RESULTS:]
        s["active"] = None
        return True


def _snapshot():
    """Thread-safe snapshot of process-wide state."""
    s = _state()
    with s["lock"]:
        active = dict(s["active"]) if s["active"] else None
        results = list(s["results"])
        last_error = dict(s["last_error"]) if s["last_error"] else None
    return active, results, last_error


# ============================================================
# Session state init + sync from process-wide state
# ============================================================
if "history" not in st.session_state:
    st.session_state.history = []
if "seen_result_ids" not in st.session_state:
    st.session_state.seen_result_ids = set()
if "seen_error_ts" not in st.session_state:
    st.session_state.seen_error_ts = 0.0

# Pull any process-wide results that this session hasn't ingested yet into local history.
# Inserting oldest-first preserves newest-first ordering at the head of the list.
_snap_active, _snap_results, _snap_error = _snapshot()
for r in reversed(_snap_results):
    if r["id"] not in st.session_state.seen_result_ids:
        st.session_state.history.insert(0, r)
        st.session_state.seen_result_ids.add(r["id"])

# ============================================================
# LIVE STATS (auto-refresh every 2s — now actually live during conversion)
#
# Layout is built ONCE at script-top so the column structure, labels, and
# placeholder slots stay in the DOM across reruns. Each 2s tick only writes
# fresh values into st.empty() slots — Streamlit's DOM diff updates only the
# changed text nodes instead of tearing down and rebuilding the whole stats
# block, which was causing visible blinking.
# ============================================================
st.title("📄 Office Convert — Dashboard")

# Built once at script-top so the column structure and metric widgets stay
# mounted across fragment reruns. Each tick rewrites only the value strings
# via placeholder.metric() — React's reconciliation preserves the surrounding
# layout, so only the changing digits are touched.
_c1, _c2, _c3, _c4, _c5 = st.columns(5)
_slot_server = _c1.empty()
_slot_cpu = _c2.empty()
_slot_mem = _c3.empty()
_slot_workers = _c4.empty()
_slot_jobs = _c5.empty()
_slot_detail = st.empty()
_slot_processes = st.empty()


# 4s instead of 2s: the docker fetch is non-blocking now (background thread),
# but every fragment tick still costs Streamlit a delta+DOM patch. Slower
# cadence makes the unavoidable patch much less visible.
@st.fragment(run_every=4)
def live_stats():
    health = get_health()
    stats = get_docker_stats()
    workers = get_worker_processes()

    _slot_server.metric("Server", "✅ Ready" if health.get("ready") else "❌ Down")
    _slot_cpu.metric("CPU", stats["cpu"])
    _slot_mem.metric("Memory", stats["mem_pct"])
    _slot_workers.metric("Workers", str(len(workers)))
    _slot_jobs.metric(
        "Jobs",
        f"{health.get('active_jobs', '?')}/{health.get('max_jobs', '?')}",
    )
    _slot_detail.caption(
        f"Mem: {stats['mem_usage']} • PIDs: {stats['pids']} • "
        f"License: {health.get('license_days_remaining', '?')} days"
    )

    if workers:
        lines = []
        for w in workers:
            parts = w.split()
            if len(parts) >= 2:
                fmt = (
                    "docx"
                    if "docx" in w
                    else "pptx"
                    if "pptx" in w
                    else "xlsx"
                    if "xlsx" in w
                    else "pdf"
                    if "pdf" in w
                    else "?"
                )
                mode = "pool" if "pool" in w else "render" if "render" in w else "probe"
                lines.append(f"⚙️ PID {parts[0]} • CPU {parts[1]}% • {fmt} • {mode}")
        _slot_processes.caption("  \n".join(lines))
    else:
        _slot_processes.empty()


def _render_heartbeats(request_id: str, wall_now: float) -> str:
    """Group heartbeats by pool_index, pick the latest per worker, render HTML."""
    beats = get_heartbeats(request_id)
    if not beats:
        return (
            '<div style="font-size:0.85rem;opacity:0.6;margin-top:0.5rem;">'
            "Pool workers: waiting for first heartbeat…"
            "</div>"
        )
    by_index: dict[int, dict] = {}
    for b in beats:
        idx = b.get("pool_index")
        if idx is None:
            continue
        prev = by_index.get(idx)
        if prev is None or (b.get("wall_ts") or 0) > (prev.get("wall_ts") or 0):
            by_index[idx] = b

    rows = []
    for idx in sorted(by_index.keys()):
        b = by_index[idx]
        wall_ts = b.get("wall_ts") or 0
        staleness = wall_now - wall_ts if wall_ts else 0
        rss_bytes = b.get("rss_bytes") or 0
        rss_mb = rss_bytes / 1024 / 1024 if rss_bytes >= 0 else 0
        swap_bytes = b.get("swap_bytes") or 0
        swap_mb = swap_bytes / 1024 / 1024 if swap_bytes >= 0 else 0
        phase = b.get("phase") or "?"
        elapsed_in_phase = b.get("elapsed_s")
        jiffies = b.get("cpu_jiffies")
        worker = b.get("worker") or "?"
        # Stale = no heartbeat in >3 × cadence. C++ default is 2s, so >6s is suspicious.
        color = "rgba(0,180,0,0.85)" if staleness < 6 else "rgba(220,90,0,0.9)"
        # Any non-zero swap is worth visual highlight — under memswap_limit
        # the worker pages out when RAM is exhausted, and chronic swap use
        # is the signal that the chunk planner is mis-sized for this input.
        swap_cell_style = (
            "padding:1px 8px;text-align:right;color:rgba(220,90,0,0.95);font-weight:600;"
            if swap_mb > 0
            else "padding:1px 8px;text-align:right;opacity:0.5;"
        )
        rows.append(
            f'<tr style="font-variant-numeric:tabular-nums;">'
            f'<td style="padding:1px 8px;color:{color};">●</td>'
            f'<td style="padding:1px 8px;">[{idx}]</td>'
            f'<td style="padding:1px 8px;">{worker}</td>'
            f'<td style="padding:1px 8px;">{phase}</td>'
            f'<td style="padding:1px 8px;text-align:right;">{elapsed_in_phase}s</td>'
            f'<td style="padding:1px 8px;text-align:right;">{rss_mb:,.0f} MB</td>'
            f'<td style="{swap_cell_style}">{swap_mb:,.0f} MB</td>'
            f'<td style="padding:1px 8px;text-align:right;">{jiffies}</td>'
            f'<td style="padding:1px 8px;text-align:right;opacity:0.6;">{staleness:.1f}s ago</td>'
            f"</tr>"
        )
    table = (
        '<table style="font-size:0.82rem;margin-top:0.5rem;border-collapse:collapse;">'
        '<thead><tr style="opacity:0.55;">'
        '<th></th><th style="padding:1px 8px;text-align:left;">#</th>'
        '<th style="padding:1px 8px;text-align:left;">worker</th>'
        '<th style="padding:1px 8px;text-align:left;">phase</th>'
        '<th style="padding:1px 8px;text-align:right;">elapsed</th>'
        '<th style="padding:1px 8px;text-align:right;">RSS</th>'
        '<th style="padding:1px 8px;text-align:right;">Swap</th>'
        '<th style="padding:1px 8px;text-align:right;">CPU jiffies</th>'
        '<th style="padding:1px 8px;text-align:right;">last hb</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
    )
    return f'<div style="margin-top:0.4rem;">Pool workers ({len(by_index)}, {len(beats)} hbs total):{table}</div>'


def _render_progress_html(p: dict) -> str:
    """HTML progress bar + phase-label row using the /jobs/{id}/progress payload."""
    pct = float(p.get("weighted_percent") or 0.0)
    pct_label = f"{int(pct * 100)}%"
    phase = p.get("phase", "init")
    total = int(p.get("total_chunks") or 0)
    done = int(p.get("chunks_rendered") or 0)
    load_pct = int(float(p.get("load_progress") or 0.0) * 100)
    merge_pct = int(float(p.get("merge_done") or 0.0) * 100)

    # Per-phase highlighting: bold the current phase, dim the others.
    def fmt(label: str, current: bool, dim: bool = False) -> str:
        if current:
            return f"<b>{label}</b>"
        if dim:
            return f'<span style="opacity:0.45;">{label}</span>'
        return label

    is_load = phase in ("init", "probing", "planning", "loading")
    is_render = phase == "rendering"
    is_merge = phase == "merging"
    is_done = phase == "complete"

    load_label = f"Load {load_pct}%"
    render_label = f"Render {done}/{total}" if total else "Render"
    merge_label = "Merge ✓" if merge_pct >= 100 else "Merge"

    phase_row = (
        f"{fmt(load_label, is_load, dim=(is_render or is_merge or is_done))} "
        f'<span style="opacity:0.55;">→</span> '
        f"{fmt(render_label, is_render, dim=(is_merge or is_done))} "
        f'<span style="opacity:0.55;">→</span> '
        f"{fmt(merge_label, is_merge or is_done, dim=False)}"
    )

    # Inline CSS progress bar — Streamlit's st.progress() lives outside markdown
    # blocks, so we render our own to keep everything in a single callout.
    bar_width = max(1, min(100, int(pct * 100)))
    bar = (
        '<div style="height:8px;background:rgba(0,120,255,0.12);'
        'border-radius:4px;overflow:hidden;margin:0.4rem 0 0.3rem 0;">'
        f'<div style="height:100%;width:{bar_width}%;'
        "background:linear-gradient(90deg, rgba(0,120,255,0.65), rgba(0,180,140,0.85));"
        'transition:width 0.5s ease-out;"></div></div>'
    )

    return (
        f'<div style="margin-top:0.4rem;font-size:0.88rem;">'
        f"{bar}"
        f'<div style="display:flex;justify-content:space-between;align-items:center;">'
        f"<span>{phase_row}</span>"
        f'<span style="font-variant-numeric:tabular-nums;font-weight:600;'
        f'opacity:0.85;">{pct_label}</span></div>'
        f"</div>"
    )


@st.fragment(run_every=1)
def conversion_status():
    active, _results, _err = _snapshot()
    if active is None:
        _slot_conv_status.empty()
        return
    if active["holder"].get("done"):
        _collect_if_finished()
        st.rerun(scope="app")
    else:
        # Integer seconds (not 0.1s) so the value changes only once per second,
        # reducing visible repaints. Tabular nums keeps digit widths constant.
        elapsed = int(time.time() - active["start_time"])
        progress_html = _render_progress_html(get_progress(active["id"]))
        heartbeats_html = _render_heartbeats(active["id"], time.time())
        html = (
            '<div style="background:rgba(0,120,255,0.08);'
            'border-left:4px solid rgba(0,120,255,0.6);'
            'padding:0.6rem 1rem;border-radius:0.4rem;'
            'font-size:0.95rem;">'
            f'⏳ Converting <b>{active["input_name"]}</b> '
            f'({active["input_size_mb"]:.2f} MB) — '
            f'<span style="font-variant-numeric:tabular-nums;">'
            f'{elapsed}s</span> elapsed. Stats refresh live above ⬆️'
            f'{progress_html}'
            f'{heartbeats_html}'
            "</div>"
        )
        _slot_conv_status.markdown(html, unsafe_allow_html=True)


live_stats()
st.divider()

# ============================================================
# CONVERSION
# ============================================================
st.header("🚀 Convert a File")

# Show pending error if recent enough and not already shown to this session
if _snap_error and _snap_error["ts"] > st.session_state.seen_error_ts:
    age = time.time() - _snap_error["ts"]
    if age <= ERROR_DISPLAY_WINDOW_SEC:
        st.error(_snap_error["msg"])
    st.session_state.seen_error_ts = _snap_error["ts"]

uploaded_file = st.file_uploader(
    "Drop a file (DOCX, PPTX, XLSX, PDF, DOC, XLS, PPT)",
    type=["docx", "pptx", "xlsx", "pdf", "doc", "xls", "ppt"],
)

# Persistent placeholder for the "⏳ Converting..." callout. Created at a
# stable DOM position so the 1s fragment updates only the inner text/icon
# instead of re-creating the surrounding alert each tick.
_slot_conv_status = st.empty()

if _snap_active is not None:
    conversion_status()
    if uploaded_file:
        st.warning("⏳ A conversion is already running — submit another after it finishes.")
elif uploaded_file:
    size_mb = len(uploaded_file.getvalue()) / 1024 / 1024
    st.info(f"📁 **{uploaded_file.name}** — {size_mb:.2f} MB")

    if st.button("▶️ Start Conversion", type="primary"):
        if _start_conversion(uploaded_file.name, uploaded_file.getvalue()):
            st.rerun()
        else:
            st.warning("Another conversion just started — try again in a moment.")

# ============================================================
# CONVERSION HISTORY
# ============================================================
if st.session_state.history:
    st.divider()
    st.header("📦 Conversion History")

    latest = st.session_state.history[0]
    st.success(
        f"🎉 **{latest['input']}** → {latest['name']} | ⏱️ **{latest['time']:.1f}s** | {latest['out_mb']:.1f} MB"
    )

    for i, item in enumerate(st.session_state.history[:10]):
        col1, col2, col3 = st.columns([4, 1, 1])
        with col1:
            st.markdown(
                f"**{item['input']}** → `{item['name']}`  \n"
                f"⏱️ {item['time']:.1f}s | 📥 {item['in_mb']:.1f} MB → 📤 {item['out_mb']:.1f} MB | 🕐 {item['ts']}"
            )
        with col2:
            st.download_button(
                "⬇️ Download",
                data=item["data"],
                file_name=item["name"],
                mime="application/pdf",
                key=f"dl_{i}_{item['ts']}",
            )
        with col3:
            st.text(f"{item['time']:.1f}s")

st.divider()
st.caption(
    "Stats auto-refresh every 2s — including during conversion. In-progress conversions and recent results survive page refresh."
)
