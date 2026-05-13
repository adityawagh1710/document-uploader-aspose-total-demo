"""
Office Convert Test UI — Monitoring + Conversion Dashboard.

- Stats refresh every 2s at the top
- Conversion with spinner (stats pause during conversion)
- Conversion history with time taken + download buttons
- Error display

Run: docker compose up -d
Open: http://localhost:8501
"""

import os
import subprocess
import time
from pathlib import Path

import streamlit as st
import requests

API_URL = os.environ.get("API_URL", "http://localhost:8080")
CONVERT_URL = f"{API_URL}/convert"
HEALTH_URL = f"{API_URL}/health"

st.set_page_config(page_title="Office Convert Dashboard", layout="wide")


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


# ============================================================
# LIVE STATS (auto-refresh every 2s)
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


live_stats()
st.divider()

# ============================================================
# CONVERSION
# ============================================================
st.header("🚀 Convert a File")

if "history" not in st.session_state:
    st.session_state.history = []

uploaded_file = st.file_uploader(
    "Drop a file (DOCX, PPTX, XLSX, PDF, DOC, XLS, PPT)",
    type=["docx", "pptx", "xlsx", "pdf", "doc", "xls", "ppt"],
)

if uploaded_file:
    size_mb = len(uploaded_file.getvalue()) / 1024 / 1024
    st.info(f"📁 **{uploaded_file.name}** — {size_mb:.2f} MB")

    if st.button("▶️ Start Conversion", type="primary"):
        with st.spinner(f"Converting {uploaded_file.name}... (stats pause during conversion)"):
            data, elapsed, error = do_conversion(uploaded_file.name, uploaded_file.getvalue())

        if error:
            st.error(f"❌ {error} (after {elapsed:.1f}s)")
        elif data:
            out_name = Path(uploaded_file.name).stem + ".pdf"
            st.session_state.history.insert(0, {
                "name": out_name,
                "input": uploaded_file.name,
                "data": data,
                "out_mb": len(data) / 1024 / 1024,
                "in_mb": size_mb,
                "time": elapsed,
                "ts": time.strftime("%H:%M:%S"),
            })
            st.rerun()

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
st.caption("Stats auto-refresh every 2s. During conversion, stats pause briefly. History persists until page reload.")
