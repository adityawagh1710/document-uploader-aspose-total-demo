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

import streamlit as st
import requests
from streamlit.runtime.scriptrunner import add_script_run_ctx

API_URL = os.environ.get("API_URL", "http://localhost:8080")
CONVERT_URL = f"{API_URL}/convert"
HEALTH_URL = f"{API_URL}/health"

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
        "active": None,        # {id, holder, thread, start_time, input_name, input_size_mb}
        "results": [],         # successful results, newest first
        "last_error": None,    # {"msg": str, "ts": float}
    }


def get_health():
    try:
        return requests.get(HEALTH_URL, timeout=3).json()
    except Exception as e:
        return {"error": str(e)}


def get_docker_stats():
    try:
        result = subprocess.run(
            ["docker", "stats", "office-convert", "--no-stream", "--format",
             "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("\t")
            return {"cpu": parts[0], "mem_usage": parts[1], "mem_pct": parts[2], "pids": parts[3]}
    except Exception:
        pass
    return {"cpu": "N/A", "mem_usage": "N/A", "mem_pct": "N/A", "pids": "N/A"}


def get_worker_processes():
    try:
        result = subprocess.run(
            ["docker", "top", "office-convert", "-o", "pid,pcpu,pmem,time,args"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return [l for l in result.stdout.strip().split("\n") if "office-convert-worker" in l]
    except Exception:
        pass
    return []


def do_conversion(file_name, file_bytes):
    """Blocking conversion. Returns (data, elapsed, error)."""
    start = time.time()
    try:
        resp = requests.post(CONVERT_URL, files={"file": (file_name, file_bytes)}, stream=True, timeout=1800)
        if resp.status_code != 200:
            try:
                body = resp.json()
                return None, time.time() - start, f"Error {resp.status_code}: {body.get('failure_class', 'unknown')}"
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


def _conversion_worker(file_name, file_bytes, holder):
    """Runs in a background thread. Writes result into shared holder dict."""
    data, elapsed, error = do_conversion(file_name, file_bytes)
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
        holder = {"done": False}
        thread = threading.Thread(
            target=_conversion_worker,
            args=(file_name, file_bytes, holder),
            daemon=True,
        )
        add_script_run_ctx(thread)
        s["active"] = {
            "id": str(uuid.uuid4()),
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
            s["results"].insert(0, {
                "id": result_id,
                "name": out_name,
                "input": input_name,
                "data": data,
                "out_mb": len(data) / 1024 / 1024,
                "in_mb": input_size_mb,
                "time": elapsed,
                "ts": time.strftime("%H:%M:%S"),
            })
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
# ============================================================
st.title("📄 Office Convert — Dashboard")


@st.fragment(run_every=2)
def live_stats():
    health = get_health()
    stats = get_docker_stats()
    workers = get_worker_processes()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Server", "✅ Ready" if health.get("ready") else "❌ Down")
    c2.metric("CPU", stats["cpu"])
    c3.metric("Memory", stats["mem_pct"])
    c4.metric("Workers", str(len(workers)))
    c5.metric("Jobs", f"{health.get('active_jobs', '?')}/{health.get('max_jobs', '?')}")

    st.text(f"Mem: {stats['mem_usage']} | PIDs: {stats['pids']} | License: {health.get('license_days_remaining', '?')} days")

    if workers:
        for w in workers:
            parts = w.split()
            if len(parts) >= 2:
                fmt = "docx" if "docx" in w else "pptx" if "pptx" in w else "xlsx" if "xlsx" in w else "pdf" if "pdf" in w else "?"
                mode = "pool" if "pool" in w else "render" if "render" in w else "probe"
                st.text(f"  ⚙️ PID {parts[0]} | CPU {parts[1]}% | {fmt} | {mode}")


@st.fragment(run_every=1)
def conversion_status():
    active, _results, _err = _snapshot()
    if active is None:
        return
    if active["holder"].get("done"):
        _collect_if_finished()
        st.rerun(scope="app")
    else:
        elapsed = time.time() - active["start_time"]
        st.info(
            f"⏳ Converting **{active['input_name']}** ({active['input_size_mb']:.2f} MB)... "
            f"({elapsed:.1f}s) — stats refresh live above ⬆️"
        )


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
    st.success(f"🎉 **{latest['input']}** → {latest['name']} | ⏱️ **{latest['time']:.1f}s** | {latest['out_mb']:.1f} MB")

    for i, item in enumerate(st.session_state.history[:10]):
        col1, col2, col3 = st.columns([4, 1, 1])
        with col1:
            st.markdown(
                f"**{item['input']}** → `{item['name']}`  \n"
                f"⏱️ {item['time']:.1f}s | 📥 {item['in_mb']:.1f} MB → 📤 {item['out_mb']:.1f} MB | 🕐 {item['ts']}"
            )
        with col2:
            st.download_button(
                f"⬇️ Download",
                data=item["data"],
                file_name=item["name"],
                mime="application/pdf",
                key=f"dl_{i}_{item['ts']}",
            )
        with col3:
            st.text(f"{item['time']:.1f}s")

st.divider()
st.caption("Stats auto-refresh every 2s — including during conversion. In-progress conversions and recent results survive page refresh.")
