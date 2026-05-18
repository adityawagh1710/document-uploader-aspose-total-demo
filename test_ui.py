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

import datetime
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

API_URL = os.environ.get("API_URL", "http://localhost:8080")
CONVERT_URL = f"{API_URL}/convert"
HEALTH_URL = f"{API_URL}/health"
HEARTBEATS_URL = f"{API_URL}/jobs"  # /jobs/{request_id}/heartbeats
PROGRESS_URL = f"{API_URL}/jobs"  # /jobs/{request_id}/progress
TIMINGS_URL = f"{API_URL}/jobs"  # /jobs/{request_id}/timings

# Distinct, high-saturation per-worker palette so up to 8 pool workers stay
# visually separable in the live charts.
WORKER_COLORS = [
    "#06b6d4",  # cyan
    "#a855f7",  # violet
    "#10b981",  # emerald
    "#f59e0b",  # amber
    "#f43f5e",  # rose
    "#0ea5e9",  # sky
    "#fbbf24",  # gold
    "#ec4899",  # pink
]

MAX_RECENT_RESULTS = 20
ERROR_DISPLAY_WINDOW_SEC = 60

st.set_page_config(page_title="Office Convert Dashboard", layout="wide")

# ============================================================
# Global dashboard CSS — dense dark-grid look matching the
# Kubernetes-style operator dashboards (KPI tiles, gauges,
# status pills, bar-cell tables).
# ============================================================
st.markdown(
    """
    <style>
      /* Hide Streamlit chrome we don't need for the dashboard look. */
      #MainMenu, footer, header[data-testid="stHeader"] { display: none !important; }
      .block-container {
        padding-top: 0.8rem !important;
        padding-bottom: 1rem !important;
        max-width: 100% !important;
      }

      /* App background — deep slate gradient. */
      .stApp {
        background:
          radial-gradient(1200px 600px at 100% 0%, rgba(56,189,248,0.05), transparent 60%),
          radial-gradient(900px 500px at 0% 100%, rgba(168,85,247,0.05), transparent 60%),
          linear-gradient(180deg, #0a0f1c 0%, #0f172a 100%);
        color: #e2e8f0;
      }

      /* Dashboard header */
      .dash-header { display: flex; align-items: baseline; gap: 12px; margin: 4px 0 14px 0; }
      .dash-header h1 {
        font-size: 18px; font-weight: 700; margin: 0;
        color: #e2e8f0; letter-spacing: -0.01em;
      }
      .dash-header .crumb {
        font-size: 11px; color: #64748b; letter-spacing: 0.04em;
      }
      .dash-header .live-dot {
        display: inline-block; width: 8px; height: 8px; border-radius: 50%;
        background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,0.6);
        animation: pulse 1.6s ease-in-out infinite;
      }
      @keyframes pulse {
        0%,100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.55; transform: scale(1.15); }
      }

      /* KPI tile row (horizontal — legacy fallback) */
      .tile-row { display: flex; gap: 12px; margin-bottom: 16px; }
      /* KPI tile stack (vertical — packs 5 tiles into the left column of
         the combined top band so the whole top reads as one block). */
      .tile-stack { display: flex; flex-direction: column; gap: 8px; }
      .tile-stack .kpi-tile { padding: 8px 12px; }
      .tile-stack .kpi-tile .label { font-size: 9.5px; margin-bottom: 2px; }
      .tile-stack .kpi-tile .value { font-size: 16px; line-height: 1.1; }
      .tile-stack .kpi-tile .sub { font-size: 9.5px; margin-top: 2px; }
      .kpi-tile {
        flex: 1; min-width: 0;
        background: linear-gradient(180deg, rgba(30,41,59,0.75), rgba(15,23,42,0.65));
        border: 1px solid rgba(148,163,184,0.12);
        border-radius: 8px;
        padding: 12px 16px;
        overflow: hidden;
      }
      .kpi-tile .label {
        font-size: 10.5px; color: #94a3b8;
        text-transform: uppercase; letter-spacing: 0.1em;
        font-weight: 600; margin-bottom: 6px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .kpi-tile .value {
        font-size: 22px; font-weight: 700; color: #e2e8f0;
        font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
        line-height: 1.1;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .kpi-tile .sub {
        font-size: 10.5px; color: #64748b;
        margin-top: 4px; font-variant-numeric: tabular-nums;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .kpi-tile.ok .value   { color: #4ade80; }
      .kpi-tile.warn .value { color: #fbbf24; }
      .kpi-tile.crit .value { color: #f87171; }
      .kpi-tile.info .value { color: #38bdf8; }

      /* Status pills */
      .pill {
        display: inline-block; padding: 2px 9px;
        border-radius: 4px; font-size: 10.5px;
        font-weight: 700; letter-spacing: 0.06em;
        text-transform: uppercase;
        font-variant-numeric: tabular-nums;
      }
      .pill.ok   { background: rgba(34,197,94,0.18);  color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
      .pill.warn { background: rgba(245,158,11,0.18); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }
      .pill.crit { background: rgba(239,68,68,0.18);  color: #f87171; border: 1px solid rgba(239,68,68,0.4); }
      .pill.info { background: rgba(56,189,248,0.18); color: #38bdf8; border: 1px solid rgba(56,189,248,0.4); }
      .pill.dim  { background: rgba(148,163,184,0.10); color: #94a3b8; border: 1px solid rgba(148,163,184,0.25); }

      /* Dashboard table */
      .dash-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
      .dash-table thead th {
        color: #64748b; font-size: 10.5px; font-weight: 600;
        text-transform: uppercase; letter-spacing: 0.06em;
        padding: 8px 10px; text-align: left;
        border-bottom: 1px solid rgba(148,163,184,0.15);
        background: rgba(15,23,42,0.45);
      }
      .dash-table tbody td {
        padding: 8px 10px;
        border-bottom: 1px solid rgba(148,163,184,0.06);
        color: #cbd5e1; font-variant-numeric: tabular-nums;
      }
      .dash-table tbody tr:hover td { background: rgba(56,189,248,0.04); }

      /* Bar cells inside tables */
      .bar-cell {
        position: relative; height: 18px;
        background: rgba(148,163,184,0.08);
        border-radius: 2px; min-width: 120px;
      }
      .bar-fill {
        height: 100%; border-radius: 2px;
        transition: width 600ms ease;
      }
      .bar-fill.cpu  { background: linear-gradient(90deg, #22d3ee, #06b6d4); }
      .bar-fill.mem  { background: linear-gradient(90deg, #84cc16, #65a30d); }
      .bar-fill.swap { background: linear-gradient(90deg, #fb923c, #ea580c); }
      .bar-label {
        position: absolute; top: 50%; transform: translateY(-50%);
        left: 8px; font-size: 11px; color: white;
        font-weight: 600; font-variant-numeric: tabular-nums;
        text-shadow: 0 1px 2px rgba(0,0,0,0.6);
      }

      /* Utilization cards — big number + gradient bar, replaces tiny donuts */
      .util-card {
        background: linear-gradient(180deg, rgba(30,41,59,0.7), rgba(15,23,42,0.55));
        border: 1px solid rgba(148,163,184,0.12);
        border-radius: 8px;
        padding: 14px 16px;
        height: 210px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 10px;
      }
      .util-card .label {
        font-size: 10.5px; color: #94a3b8;
        text-transform: uppercase; letter-spacing: 0.08em;
        font-weight: 600;
      }
      .util-card .value {
        font-size: 36px; font-weight: 700; color: #e2e8f0;
        font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
        line-height: 1;
      }
      .util-card .bar {
        height: 10px; background: rgba(148,163,184,0.12);
        border-radius: 5px; overflow: hidden;
      }
      .util-card .bar-fill {
        height: 100%; border-radius: 5px;
        transition: width 600ms cubic-bezier(0.4, 0, 0.2, 1);
      }
      .util-card .bar-fill.cpu  { background: linear-gradient(90deg, #22d3ee, #06b6d4); }
      .util-card .bar-fill.mem  { background: linear-gradient(90deg, #84cc16, #65a30d); }
      .util-card .bar-fill.warn { background: linear-gradient(90deg, #fbbf24, #f59e0b); }
      .util-card .bar-fill.crit { background: linear-gradient(90deg, #f87171, #ef4444); }
      .util-card .meta {
        font-size: 10.5px; color: #64748b;
        font-variant-numeric: tabular-nums;
        display: flex; justify-content: space-between;
      }

      /* Section header */
      .section-hdr {
        font-size: 11px; color: #94a3b8;
        text-transform: uppercase; letter-spacing: 0.08em;
        font-weight: 700; margin: 18px 0 6px 0;
        padding-bottom: 4px;
        border-bottom: 1px solid rgba(148,163,184,0.12);
        display: flex; align-items: center; gap: 10px;
      }
      .section-hdr .right { margin-left: auto; font-weight: 500;
        text-transform: none; letter-spacing: 0; color: #64748b; }

      /* Tighter spacing for nested Streamlit elements inside dashboard */
      [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(0,0,0,0) !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Dashboard helpers — tiles, gauges, pills, bars
# ============================================================
def _render_tile(label: str, value: str, status: str = "info", sub: str = "") -> str:
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi-tile {status}">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        f"{sub_html}"
        f"</div>"
    )


def _render_tile_row(tiles: list[str], *, stacked: bool = False) -> str:
    """Render 5 KPI tiles as a flex row (default) or a vertical stack.

    The horizontal `tile-row` is the default now — Row 1 of the dashboard
    spans the full width with all 5 tiles inline. `stacked=True` packs the
    tiles vertically for narrow side columns (kept for future layouts)."""
    cls = "tile-stack" if stacked else "tile-row"
    return f'<div class="{cls}">' + "".join(tiles) + "</div>"


def _render_util_card(label: str, pct: float, base_class: str = "cpu", sub: str = "") -> str:
    """Big number + colored progress bar, replaces the tiny plotly donut.

    Bar colour shifts to warn / crit when utilisation crosses 70 / 90 %.
    Works at any width — the donut needed >150 px to look right; this
    looks identical at 80 px or 400 px.
    """
    p = max(0.0, min(100.0, float(pct)))
    if p >= 90:
        fill_class = "crit"
    elif p >= 70:
        fill_class = "warn"
    else:
        fill_class = base_class
    sub_html = f'<div class="meta"><span>0%</span><span>{sub}</span><span>100%</span></div>' if sub else \
               '<div class="meta"><span>0%</span><span>100%</span></div>'
    return (
        f'<div class="util-card">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{p:.1f}%</div>'
        f'<div class="bar"><div class="bar-fill {fill_class}" style="width:{p}%"></div></div>'
        f'{sub_html}'
        f'</div>'
    )


def _build_gauge(value: float, label: str, color: str = "#22d3ee") -> "go.Figure":
    """Donut-style utilization gauge with the percent in the centre.

    Steps colour the arc background by zone (green / amber / red) so a
    high-utilization workload is immediately legible without reading the
    number.
    """
    # Clamp + colour the bar by zone so the at-a-glance read tracks danger.
    v = max(0.0, min(100.0, float(value)))
    bar_color = color if v < 70 else "#f59e0b" if v < 90 else "#ef4444"
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=v,
            number={
                "suffix": "%",
                "font": {"size": 34, "color": "#e2e8f0"},
                "valueformat": ".1f",
            },
            gauge={
                "axis": {
                    "range": [0, 100],
                    "tickfont": {"size": 9, "color": "#475569"},
                    "tickcolor": "#475569",
                    "tickvals": [0, 50, 100],
                    "ticks": "inside",
                },
                "bar": {"color": bar_color, "thickness": 0.32},
                "bgcolor": "rgba(15,23,42,0.5)",
                "borderwidth": 0,
                "steps": [
                    {"range": [0, 70], "color": "rgba(34,197,94,0.05)"},
                    {"range": [70, 90], "color": "rgba(245,158,11,0.08)"},
                    {"range": [90, 100], "color": "rgba(239,68,68,0.12)"},
                ],
                "threshold": {
                    "line": {"color": "rgba(226,232,240,0.5)", "width": 1},
                    "thickness": 0.7,
                    "value": v,
                },
            },
        )
    )
    fig.update_layout(
        title={
            "text": label,
            "font": {"size": 11, "color": "#94a3b8"},
            "x": 0.5,
            "xanchor": "center",
            "y": 0.93,
        },
        height=210,
        margin=dict(l=18, r=18, t=42, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


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

    On Kubernetes there's no docker socket — every subprocess call would just
    time out at 5 s and return nothing. Skip spawning the loop entirely when
    /var/run/docker.sock is absent.
    """
    state: dict = {
        "lock": threading.Lock(),
        "stats": dict(_DEFAULT_DOCKER_STATS),
        "workers": [],
    }

    if not Path("/var/run/docker.sock").exists():
        return state

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


def get_timings(request_id: str) -> list[dict]:
    """Fetch the stage-timing trail (load / paginate / save events per worker)."""
    try:
        resp = requests.get(f"{TIMINGS_URL}/{request_id}/timings", timeout=2)
        if resp.status_code != 200:
            return []
        return resp.json().get("timings", []) or []
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
# Dashboard header — title + live indicator (pulses while stats are refreshing).
st.markdown(
    '<div class="dash-header">'
    '<h1>📄 Office-Convert</h1>'
    '<span class="crumb">Monitor &nbsp;›&nbsp; Conversion service</span>'
    '<span class="crumb" style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;">'
    '<span class="live-dot"></span>LIVE</span>'
    '</div>',
    unsafe_allow_html=True,
)

# Row 1: 5 KPI status tiles laid out horizontally across the full width.
_slot_tiles = st.empty()

# Row 2 — the Mega Row of live stats. Left → right: CPU · RAM · workers ·
# memory · timing · Gantt. Tiles moved back up to Row 1 so each panel
# here has more breathing room (12.5% of width per column on avg).
_gcol1, _gcol2, _gcol3, _gcol4, _gcol5, _gcol6 = st.columns(
    [0.8, 0.8, 1.7, 1.9, 1.7, 1.9], vertical_alignment="top"
)
_slot_cpu_gauge = _gcol1.empty()
_slot_ram_gauge = _gcol2.empty()
_slot_proc_panel = _gcol3.empty()
_slot_chart_mem = _gcol4.empty()
_slot_chart_tim = _gcol5.empty()
_slot_chart_gantt = _gcol6.empty()


@st.fragment(run_every=4)
def live_stats():
    health = get_health()
    stats = get_docker_stats()
    workers = get_worker_processes()

    # --- Status tile row ---------------------------------------------------
    ready = bool(health.get("ready"))
    license_days = int(health.get("license_days_remaining") or 0)
    license_status = "ok" if license_days > 30 else ("warn" if license_days > 7 else "crit")
    active_jobs = health.get("active_jobs", 0)
    max_jobs = health.get("max_jobs", 0)
    jobs_status = "ok" if (max_jobs and active_jobs < max_jobs) else "warn"
    worker_count = len(workers)
    worker_status = "info" if worker_count > 0 else "dim"

    tiles_html = _render_tile_row([
        _render_tile(
            "Service",
            "OK" if ready else "DOWN",
            "ok" if ready else "crit",
            sub="office-convert · port 8080",
        ),
        _render_tile(
            "Pool Mode",
            "fork",
            "info",
            sub="legacy pool for XLSX",
        ),
        _render_tile(
            "License",
            f"{license_days} days",
            license_status,
            sub="Aspose.Total — auto-renews on rebuild",
        ),
        _render_tile(
            "API Health",
            "READY" if ready else "503",
            "ok" if ready else "crit",
            sub=", ".join(health.get("problems") or []) or "no problems",
        ),
        _render_tile(
            "Active Jobs",
            f"{active_jobs} / {max_jobs}" if max_jobs else "—",
            jobs_status,
            sub=f"{worker_count} worker PIDs alive",
        ),
    ])
    _slot_tiles.markdown(tiles_html, unsafe_allow_html=True)

    # --- Gauges + process panel -------------------------------------------
    cpu_pct_str = str(stats.get("cpu", "0%")).rstrip("%").strip() or "0"
    mem_pct_str = str(stats.get("mem_pct", "0%")).rstrip("%").strip() or "0"
    try:
        cpu_pct = float(cpu_pct_str)
    except ValueError:
        cpu_pct = 0.0
    try:
        mem_pct = float(mem_pct_str)
    except ValueError:
        mem_pct = 0.0

    _slot_cpu_gauge.markdown(
        _render_util_card("CPU Utilization", cpu_pct, "cpu"),
        unsafe_allow_html=True,
    )
    _slot_ram_gauge.markdown(
        _render_util_card(
            "Memory Utilization",
            mem_pct,
            "mem",
            sub=str(stats.get("mem_usage", "")),
        ),
        unsafe_allow_html=True,
    )

    # Process activity panel — replaces the old caption row with a dashboard
    # table showing every live worker PID inside the office-convert container.
    if workers:
        rows = []
        for w in workers:
            parts = w.split()
            if len(parts) < 2:
                continue
            pid = parts[0]
            cpu_str = parts[1]
            fmt = (
                "docx" if "docx" in w
                else "pptx" if "pptx" in w
                else "xlsx" if "xlsx" in w
                else "pdf" if "pdf" in w
                else "—"
            )
            mode = "pool" if "pool" in w else "render" if "render" in w else "probe"
            try:
                cpu_val = float(cpu_str.rstrip("%"))
            except ValueError:
                cpu_val = 0.0
            cpu_pct_w = max(0.0, min(100.0, cpu_val))
            rows.append(
                f'<tr>'
                f'<td><span class="pill info">{fmt}</span></td>'
                f'<td><span class="pill dim">{mode}</span></td>'
                f'<td>{pid}</td>'
                f'<td>'
                f'  <div class="bar-cell"><div class="bar-fill cpu" style="width:{cpu_pct_w}%"></div>'
                f'  <span class="bar-label">{cpu_val:.1f}%</span></div>'
                f'</td>'
                f'</tr>'
            )
        panel_html = (
            '<div style="background:linear-gradient(180deg,rgba(30,41,59,0.55),rgba(15,23,42,0.5));'
            'border:1px solid rgba(148,163,184,0.12);border-radius:8px;padding:10px 12px;'
            'height:210px;overflow-y:auto;">'
            '<div style="font-size:10.5px;color:#94a3b8;text-transform:uppercase;'
            'letter-spacing:0.08em;font-weight:700;margin-bottom:6px;">'
            f'Worker Processes <span style="color:#64748b;font-weight:400;'
            f'text-transform:none;letter-spacing:0;">({len(rows)})</span></div>'
            '<table class="dash-table"><thead><tr>'
            '<th>format</th><th>mode</th><th>pid</th><th>cpu</th>'
            '</tr></thead><tbody>'
            f'{"".join(rows)}'
            '</tbody></table></div>'
        )
        _slot_proc_panel.markdown(panel_html, unsafe_allow_html=True)
    else:
        idle_html = (
            '<div style="background:linear-gradient(180deg,rgba(30,41,59,0.55),rgba(15,23,42,0.5));'
            'border:1px solid rgba(148,163,184,0.12);border-radius:8px;padding:10px 12px;'
            'height:210px;display:flex;align-items:center;justify-content:center;'
            'color:#64748b;font-size:12px;">'
            '<div style="text-align:center;">'
            '<div style="font-size:24px;margin-bottom:6px;opacity:0.4;">⚙️</div>'
            'No worker processes — system idle'
            '</div></div>'
        )
        _slot_proc_panel.markdown(idle_html, unsafe_allow_html=True)


def _render_heartbeats(request_id: str, wall_now: float) -> str:
    """Dashboard-style worker pool table: status pill + RSS bar + swap bar + CPU.

    Replaces the older plain-text snapshot with a denser tabular view that
    matches the Kubernetes-Cluster operator dashboard aesthetic — green OK
    pills when fresh, amber STALE pills when no heartbeat in >6s, and
    inline progress bars for RSS and swap.
    """
    beats = get_heartbeats(request_id)
    if not beats:
        return (
            '<div style="margin-top:8px;display:flex;align-items:center;gap:8px;">'
            '<span class="pill dim">WAITING</span>'
            '<span style="font-size:11.5px;color:#64748b;">for first pool-worker heartbeat…</span>'
            '</div>'
        )

    by_index: dict[int, dict] = {}
    for b in beats:
        idx = b.get("pool_index")
        if idx is None:
            continue
        prev = by_index.get(idx)
        if prev is None or (b.get("wall_ts") or 0) > (prev.get("wall_ts") or 0):
            by_index[idx] = b

    # Bars scale relative to the largest current RSS so the visual ordering
    # is meaningful even before we hit the container memory limit.
    max_rss_mb = max(
        ((b.get("rss_bytes") or 0) / 1024 / 1024) for b in by_index.values()
    ) or 1.0

    rows: list[str] = []
    for idx in sorted(by_index.keys()):
        b = by_index[idx]
        wall_ts = b.get("wall_ts") or 0
        staleness = (wall_now - wall_ts) if wall_ts else 0
        rss_mb = (b.get("rss_bytes") or 0) / 1024 / 1024
        swap_mb = (b.get("swap_bytes") or 0) / 1024 / 1024
        phase = (b.get("phase") or "—").lower()
        elapsed = b.get("elapsed_s") or 0
        worker = b.get("worker") or "—"

        status_pill = (
            '<span class="pill ok">OK</span>' if staleness < 6
            else '<span class="pill warn">STALE</span>'
        )
        phase_pill = (
            '<span class="pill info">LOAD</span>' if phase == "load"
            else '<span class="pill ok">RENDER</span>' if phase == "render"
            else f'<span class="pill dim">{phase.upper()}</span>'
        )
        rss_pct = min(100.0, (rss_mb / max_rss_mb) * 100.0) if max_rss_mb > 0 else 0.0
        # Swap bar saturates at 500 MB — anything beyond that is already a
        # red-flag situation and the bar capping out is the desired signal.
        swap_pct = min(100.0, (swap_mb / 500.0) * 100.0) if swap_mb > 0 else 0.0
        swap_cell = (
            f'<div class="bar-cell"><div class="bar-fill swap" style="width:{swap_pct}%"></div>'
            f'<span class="bar-label">{swap_mb:,.0f} MB</span></div>'
        ) if swap_mb > 0 else '<span style="opacity:0.35;">—</span>'

        rows.append(
            f'<tr>'
            f'<td>pool[{idx}]</td>'
            f'<td>{status_pill}</td>'
            f'<td><span class="pill dim">{worker}</span></td>'
            f'<td>{phase_pill}</td>'
            f'<td style="text-align:right;">{elapsed}s</td>'
            f'<td><div class="bar-cell"><div class="bar-fill mem" style="width:{rss_pct}%"></div>'
            f'<span class="bar-label">{rss_mb:,.0f} MB</span></div></td>'
            f'<td>{swap_cell}</td>'
            f'<td style="text-align:right;color:#64748b;">{staleness:.1f}s</td>'
            f'</tr>'
        )

    table = (
        '<div class="section-hdr" style="margin-top:10px;">'
        f'POOL WORKERS <span class="right">{len(by_index)} workers · {len(beats)} heartbeats total</span>'
        '</div>'
        '<table class="dash-table">'
        '<thead><tr>'
        '<th>#</th><th>status</th><th>worker</th><th>phase</th>'
        '<th style="text-align:right;">elapsed</th>'
        '<th>RSS</th><th>swap</th>'
        '<th style="text-align:right;">last hb</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
    )
    return table


# ============================================================
# Live Plotly charts
# ============================================================
def _build_memory_chart(request_id: str) -> go.Figure | None:
    """Multi-line chart: RSS (solid) + Swap (dotted) per pool worker over time.

    Source: /jobs/{id}/heartbeats. Refresh cadence comes from the enclosing
    Streamlit fragment, not from a polling loop here.
    """
    beats = get_heartbeats(request_id)
    if not beats:
        return None
    by_idx: dict[int, list[dict]] = {}
    for b in beats:
        idx = b.get("pool_index")
        if idx is None:
            continue
        by_idx.setdefault(int(idx), []).append(b)
    if not by_idx:
        return None

    t0 = min((b.get("wall_ts") or 0) for b in beats)
    fig = go.Figure()
    any_swap = False
    for idx in sorted(by_idx):
        bs = sorted(by_idx[idx], key=lambda b: b.get("wall_ts") or 0)
        color = WORKER_COLORS[idx % len(WORKER_COLORS)]
        xs = [(b.get("wall_ts") or 0) - t0 for b in bs]
        ys_rss = [(b.get("rss_bytes") or 0) / 1024 / 1024 for b in bs]
        ys_swap = [(b.get("swap_bytes") or 0) / 1024 / 1024 for b in bs]
        phases = [b.get("phase") or "?" for b in bs]
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys_rss,
                mode="lines",
                name=f"pool[{idx}] RSS",
                line=dict(color=color, width=2.6, shape="spline", smoothing=0.6),
                hovertemplate=(
                    f"<b>pool[{idx}]</b><br>"
                    "%{x:.0f}s · %{y:.0f} MB RSS<br>phase %{customdata}<extra></extra>"
                ),
                customdata=phases,
            )
        )
        if any(s > 0 for s in ys_swap):
            any_swap = True
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys_swap,
                    mode="lines",
                    name=f"pool[{idx}] swap",
                    line=dict(color=color, width=1.5, dash="dot"),
                    hovertemplate=(
                        f"<b>pool[{idx}] swap</b><br>"
                        "%{x:.0f}s · %{y:.0f} MB<extra></extra>"
                    ),
                )
            )

    title_suffix = " · swap!" if any_swap else ""
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=f"💾 Memory over time{title_suffix}", font=dict(size=12)),
        xaxis=dict(
            title=None,
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            zerolinecolor="rgba(255,255,255,0.18)",
            tickfont=dict(size=9),
        ),
        yaxis=dict(
            title=None,
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            zerolinecolor="rgba(255,255,255,0.18)",
            tickfont=dict(size=9),
            ticksuffix=" MB",
        ),
        height=210,
        margin=dict(l=8, r=14, t=36, b=24),
        plot_bgcolor="rgba(15,23,42,0.7)",
        paper_bgcolor="rgba(15,23,42,0)",
        hovermode="x unified",
        showlegend=False,
        transition=dict(duration=400, easing="cubic-in-out"),
    )
    return fig


def _build_timing_chart(request_id: str) -> go.Figure | None:
    """Horizontal stacked bar: time per stage per pool worker.

    Source: /jobs/{id}/timings — the new stage events emitted by xlsx.cpp
    (load, paginate, chunk-reload, save). Shows definitively where each
    worker's time went, post-conversion.
    """
    events = get_timings(request_id)
    if not events:
        return None

    # Sum durations per (pool_index, stage). Skip the .summary rollup —
    # we already have the breakdown in the component stages.
    by_idx: dict[int, dict[str, float]] = {}
    for ev in events:
        stage = ev.get("stage")
        if not stage or stage.endswith(".summary"):
            continue
        idx = ev.get("pool_index")
        if idx is None:
            continue
        ms = ev.get("duration_ms") or 0
        by_idx.setdefault(int(idx), {})
        by_idx[int(idx)][stage] = by_idx[int(idx)].get(stage, 0.0) + float(ms)

    if not by_idx:
        return None

    stage_order = [
        "pool_load.workbook_load",
        "pool_load.pagination",
        "pool_render.workbook_load",
        "pool_render.save",
    ]
    stage_color = {
        "pool_load.workbook_load":   "#3b82f6",  # blue   — initial file load
        "pool_load.pagination":      "#f59e0b",  # amber  — pagination pass
        "pool_render.workbook_load": "#fb923c",  # orange — per-chunk reload
        "pool_render.save":          "#10b981",  # emerald — actual render
    }
    stage_label = {
        "pool_load.workbook_load":   "1. Initial load",
        "pool_load.pagination":      "2. Pagination",
        "pool_render.workbook_load": "3. Chunk reload",
        "pool_render.save":          "4. Render (save)",
    }

    workers = sorted(by_idx)
    y_labels = [f"pool[{idx}]" for idx in workers]
    fig = go.Figure()
    for stage in stage_order:
        xs_sec = [by_idx[idx].get(stage, 0.0) / 1000.0 for idx in workers]
        if all(x <= 0 for x in xs_sec):
            continue
        fig.add_trace(
            go.Bar(
                y=y_labels,
                x=xs_sec,
                name=stage_label[stage],
                orientation="h",
                marker=dict(
                    color=stage_color[stage],
                    line=dict(color="rgba(255,255,255,0.08)", width=1),
                ),
                text=[f"{x:.1f}s" if x > 0.5 else "" for x in xs_sec],
                textposition="inside",
                insidetextanchor="middle",
                textfont=dict(color="white", size=12),
                hovertemplate=(
                    f"<b>{stage_label[stage]}</b><br>"
                    "%{y}: %{x:.2f} s<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        template="plotly_dark",
        title=dict(text="⏱️ Time per stage", font=dict(size=12)),
        barmode="stack",
        xaxis=dict(
            title=None,
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            zerolinecolor="rgba(255,255,255,0.18)",
            ticksuffix="s",
            tickfont=dict(size=9),
        ),
        yaxis=dict(title=None, autorange="reversed", tickfont=dict(size=9)),
        height=210,
        margin=dict(l=42, r=14, t=36, b=24),
        plot_bgcolor="rgba(15,23,42,0.7)",
        paper_bgcolor="rgba(15,23,42,0)",
        showlegend=False,
        transition=dict(duration=500, easing="cubic-in-out"),
    )
    return fig


def _build_chunk_gantt(request_id: str) -> "go.Figure | None":
    """Gantt chart of each worker's stages along a real-time axis.

    The most diagnostic view for parallel-pool debugging: makes it
    instantly obvious whether workers actually overlap (good) or
    whether one is stalling while others sit idle (bad).
    """
    events = get_timings(request_id)
    if not events:
        return None

    stage_label = {
        "pool_load.workbook_load":   "Initial load",
        "pool_load.pagination":      "Pagination",
        "pool_render.workbook_load": "Chunk reload",
        "pool_render.save":          "Render (save)",
    }
    stage_color_map = {
        "Initial load":  "#3b82f6",
        "Pagination":    "#f59e0b",
        "Chunk reload":  "#fb923c",
        "Render (save)": "#10b981",
    }

    rows: list[dict] = []
    for ev in events:
        stage = ev.get("stage")
        if stage not in stage_label:
            continue
        idx = ev.get("pool_index")
        ts = ev.get("wall_ts") or 0
        dur_ms = ev.get("duration_ms") or 0
        if idx is None or ts == 0 or dur_ms <= 0:
            continue
        # wall_ts is when the event was emitted (end of stage).
        # Reconstruct start = end - duration.
        end_unix = float(ts)
        start_unix = end_unix - dur_ms / 1000.0
        rows.append(
            {
                "Worker": f"pool[{int(idx)}]",
                "Stage": stage_label[stage],
                "Start": datetime.datetime.fromtimestamp(start_unix),
                "End":   datetime.datetime.fromtimestamp(end_unix),
                "DurationLabel": f"{dur_ms / 1000:.2f}s",
            }
        )
    if not rows:
        return None

    df = pd.DataFrame(rows).sort_values("Worker")
    fig = px.timeline(
        df,
        x_start="Start",
        x_end="End",
        y="Worker",
        color="Stage",
        color_discrete_map=stage_color_map,
        hover_data={"DurationLabel": True, "Start": False, "End": False},
    )
    # Plotly timeline lays the first row at the top by reversing the y axis,
    # which we want — workers in pool order, pool[0] on top.
    fig.update_yaxes(autorange="reversed")
    fig.update_traces(marker=dict(line=dict(color="rgba(255,255,255,0.1)", width=1)))
    fig.update_layout(
        template="plotly_dark",
        title=dict(text="📊 Chunk Gantt", font=dict(size=12)),
        height=210,
        margin=dict(l=52, r=14, t=36, b=28),
        plot_bgcolor="rgba(15,23,42,0.7)",
        paper_bgcolor="rgba(15,23,42,0)",
        bargap=0.35,
        showlegend=False,
        xaxis=dict(tickfont=dict(size=9)),
        yaxis=dict(tickfont=dict(size=9)),
    )
    return fig


# ============================================================
# Live events feed — Kubernetes "Cluster problems"-style table
# ============================================================
@st.cache_data(ttl=2)
def get_recent_events(limit: int = 25) -> list[dict]:
    """Pull and parse the last N relevant events from office-convert logs.

    Filters out heartbeat noise. Returned newest-first so the feed reads
    like an alert log.
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "--tail=200", "--no-color", "office-convert"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception:
        return []
    lines = result.stdout.splitlines()
    # Match the human log format:
    #   office-convert  | YYYY-MM-DD HH:MM:SS LEVEL  [req_xxx] event_name fields...
    pat = re.compile(
        r"office-convert\s+\|\s+(\S+)\s+(\S+)\s+(\w+)\s+\[(\S+?)\]\s+(\w+)\s*(.*)$"
    )
    SKIP_EVENTS = {"pool_worker_heartbeat"}
    events: list[dict] = []
    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        _date, ts, level, rid, event, rest = m.groups()
        if event in SKIP_EVENTS:
            continue
        events.append(
            {
                "ts": ts,
                "level": level.upper(),
                "rid": rid,
                "event": event,
                "rest": rest.strip(),
            }
        )
    # Newest first, capped.
    return list(reversed(events[-limit:]))


def _render_events_feed() -> str:
    """Render the events feed as a dashboard-styled table."""
    events = get_recent_events(limit=25)
    if not events:
        return (
            '<div style="background:linear-gradient(180deg,rgba(30,41,59,0.55),rgba(15,23,42,0.5));'
            'border:1px solid rgba(148,163,184,0.12);border-radius:8px;padding:18px;'
            'text-align:center;color:#64748b;font-size:12px;">'
            '<div style="font-size:22px;opacity:0.4;">📜</div>'
            'No events yet — kick off a conversion to populate the feed.'
            '</div>'
        )

    LEVEL_PILL = {
        "INFO":    "info",
        "WARNING": "warn",
        "WARN":    "warn",
        "ERROR":   "crit",
        "DEBUG":   "dim",
    }
    EVENT_ICON = {
        "request_received":    "📨",
        "format_detected":     "🏷️",
        "probe_start":         "🔍",
        "probe_complete":      "✓",
        "plan_complete":       "📐",
        "dispatch_mode":       "🚦",
        "pool_worker_spawn":   "⚙️",
        "pool_worker_loaded":  "✅",
        "fork_pool_spawn":     "⚙️",
        "fork_pool_loaded":    "✅",
        "merge_start":         "🔗",
        "merge_complete":      "✅",
        "request_complete":    "🎉",
        "cache_hit":           "📦",
        "server_start":        "🚀",
        "worker_exit":         "🔴",
        "pool_worker_timing":  "⏱️",
        "adaptive_chunk_sizing": "✂️",
        "replan_from_pool":    "♻️",
    }

    rows: list[str] = []
    for ev in events:
        pill = LEVEL_PILL.get(ev["level"], "dim")
        icon = EVENT_ICON.get(ev["event"], "·")
        # Short request_id for legibility (req_005d1e53 → 005d1e53)
        rid_short = ev["rid"].replace("req_", "")[:8] if ev["rid"] != "-" else "—"
        rest = ev["rest"] or ""
        # Truncate very long event-field strings so the table stays tidy.
        if len(rest) > 90:
            rest = rest[:90] + "…"
        rows.append(
            f'<tr>'
            f'<td style="color:#64748b;font-size:11.5px;">{ev["ts"]}</td>'
            f'<td><span class="pill {pill}">{ev["level"]}</span></td>'
            f'<td style="color:#94a3b8;font-family:ui-monospace,monospace;font-size:11.5px;">{rid_short}</td>'
            f'<td><span style="opacity:0.7;">{icon}</span>&nbsp;'
            f'<span style="color:#e2e8f0;font-weight:500;">{ev["event"]}</span></td>'
            f'<td style="color:#94a3b8;font-size:11.5px;">{rest}</td>'
            f'</tr>'
        )

    table = (
        '<div style="background:linear-gradient(180deg,rgba(30,41,59,0.55),rgba(15,23,42,0.5));'
        'border:1px solid rgba(148,163,184,0.12);border-radius:8px;padding:0;overflow:hidden;">'
        '<table class="dash-table">'
        '<thead><tr>'
        '<th>time</th><th>level</th><th>request</th><th>event</th><th>fields</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table></div>'
    )
    return table


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


# ============================================================
# CONVERSION STATS — moved above the file uploader so the live
# charts + active-conversion progress are visible without scrolling.
# Slots are declared here (stable DOM position at the top); the
# fragment writes update them whether called from this section or
# from the action section below.
# ============================================================
def _render_history_picker() -> None:
    """Show a select box of recent conversions; lets the user freeze the
    charts on a specific past run instead of always tracking the active /
    most-recent one. The select box's value persists in session_state under
    its `key`, which the fragment then reads."""
    active, results, _err = _snapshot()
    if not results and active is None:
        return

    options: list[tuple[str, str]] = [("__auto__", "🔄 Auto · live / most recent")]
    for r in results[:10]:
        rid = r.get("id")
        if not rid:
            continue
        options.append(
            (
                rid,
                f"⏱️ {r['input']} · {r['time']:.1f}s · {r['ts']}",
            )
        )
    if len(options) <= 1:
        return

    st.selectbox(
        "Conversion to view",
        options=[opt[0] for opt in options],
        format_func=lambda v: dict(options)[v],
        key="chart_history_picker",
        label_visibility="collapsed",
    )


# Active conversion status panel — progress bar + phase + heartbeats table.
_slot_conv_status = st.empty()

# History selector (renders only when at least one completed conversion).
_render_history_picker()

# Chart label slot — sits below the Mega Row to identify which conversion
# all those live panels are tracking (📊 Live · req_xxx · filename, or
# 📌 Pinned when the history picker has overridden auto-pick).
_slot_chart_label = st.empty()


st.divider()

# ============================================================
# CONVERSION — file uploader + start button
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


@st.fragment(run_every=2)
def live_charts():
    """Live Plotly charts driven by /jobs/{id}/heartbeats + /jobs/{id}/timings.

    Priority:
      1. Explicit selection from the history picker (if user picked one).
      2. Active conversion (live, refreshing every 2s).
      3. Most recent completed conversion (data kept 30 min by the backend).

    Per-chart stable placeholder slots persist the DOM element across
    fragment reruns. `uirevision` on the figure layout preserves Plotly's
    interactive state (zoom, pan, hover) so the chart doesn't visually
    reset on data updates.
    """
    active, _results, _e = _snapshot()

    explicit = st.session_state.get("chart_history_picker")
    if explicit and explicit != "__auto__":
        rid = explicit
        # Look up the matching history record for a nice label.
        match = next((r for r in _results if r.get("id") == rid), None)
        if match is not None:
            label = (
                f'<div style="font-size:0.85rem;opacity:0.75;margin-top:0.6rem;'
                f'margin-bottom:-0.3rem;">📌 Pinned · '
                f'<code style="font-size:0.8rem;">{rid}</code> · '
                f'{match["input"]} · {match["time"]:.1f}s</div>'
            )
        else:
            label = (
                f'<div style="font-size:0.85rem;opacity:0.75;margin-top:0.6rem;'
                f'margin-bottom:-0.3rem;">📌 Pinned · '
                f'<code style="font-size:0.8rem;">{rid}</code></div>'
            )
    elif active is not None:
        rid = active["id"]
        label = (
            f'<div style="font-size:0.85rem;opacity:0.7;margin-top:0.6rem;'
            f'margin-bottom:-0.3rem;">📊 Live · '
            f'<code style="font-size:0.8rem;">{rid}</code> · '
            f'{active["input_name"]}</div>'
        )
    elif _results:
        last = _results[0]
        rid = last.get("id")
        if not rid:
            return
        label = (
            f'<div style="font-size:0.85rem;opacity:0.7;margin-top:0.6rem;'
            f'margin-bottom:-0.3rem;">📊 Last completed · '
            f'<code style="font-size:0.8rem;">{rid}</code> · '
            f'{last["input"]} · {last["time"]:.1f}s · '
            f'<span style="opacity:0.6;">data kept for 30 min</span></div>'
        )
    else:
        return

    _slot_chart_label.markdown(label, unsafe_allow_html=True)

    fig_mem = _build_memory_chart(rid)
    if fig_mem is not None:
        fig_mem.update_layout(uirevision="mem-chart")
        _slot_chart_mem.plotly_chart(
            fig_mem,
            width="stretch",
            key="chart_mem",
            config={"displayModeBar": False},
        )

    fig_tim = _build_timing_chart(rid)
    if fig_tim is not None:
        fig_tim.update_layout(uirevision="tim-chart")
        _slot_chart_tim.plotly_chart(
            fig_tim,
            width="stretch",
            key="chart_tim",
            config={"displayModeBar": False},
        )

    fig_gantt = _build_chunk_gantt(rid)
    if fig_gantt is not None:
        fig_gantt.update_layout(uirevision="gantt-chart")
        _slot_chart_gantt.plotly_chart(
            fig_gantt,
            width="stretch",
            key="chart_gantt",
            config={"displayModeBar": False},
        )


# Stats display — drives the slots that live in the top section. Called
# from here so the fragment definitions earlier in the file are in scope.
if _snap_active is not None:
    conversion_status()
    live_charts()
elif _snap_results:
    # No live conversion, but a recent one finished — show its charts.
    # The backend retains heartbeats + timings for 30 minutes after
    # completion, so the user can review what just happened.
    live_charts()

# Action block — only the upload/start UI lives below the file picker.
# Stats display above is independent of upload state.
if uploaded_file:
    if _snap_active is not None:
        st.warning(
            "⏳ A conversion is already running — submit another after it finishes."
        )
    else:
        size_mb = len(uploaded_file.getvalue()) / 1024 / 1024
        st.info(f"📁 **{uploaded_file.name}** — {size_mb:.2f} MB")

        if st.button("▶️ Start Conversion", type="primary"):
            if _start_conversion(uploaded_file.name, uploaded_file.getvalue()):
                st.rerun()
            else:
                st.warning(
                    "Another conversion just started — try again in a moment."
                )

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

# ============================================================
# LIVE EVENTS FEED — Kubernetes "Cluster problems"-style panel at the
# very bottom of the dashboard. Auto-refreshes every 3 seconds with
# the latest 25 non-heartbeat events from office-convert's structured
# log, color-coded by severity.
# ============================================================
st.markdown(
    '<div class="section-hdr">EVENTS FEED'
    '<span class="right">last 25 · refresh every 3 s</span>'
    '</div>',
    unsafe_allow_html=True,
)
_slot_events = st.empty()


@st.fragment(run_every=3)
def live_events():
    _slot_events.markdown(_render_events_feed(), unsafe_allow_html=True)


live_events()

st.markdown(
    '<div style="text-align:center;font-size:11px;color:#475569;margin-top:18px;">'
    'Stats auto-refresh every 2s · in-progress conversions and recent results '
    'survive page refresh · heartbeats &amp; timings kept for 30 min per request'
    '</div>',
    unsafe_allow_html=True,
)
