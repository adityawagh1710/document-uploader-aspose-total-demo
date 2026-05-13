"""
Office Convert Test UI — Real-time monitoring dashboard.

Only the stats section refreshes every 2 seconds (using st.fragment).
The rest of the page stays stable — no flickering during upload/conversion.

Run:
    docker compose --profile ui up -d test-ui
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
        r = requests.get(HEALTH_URL, timeout=3)
        return r.json()
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
            return {
                "cpu": parts[0] if len(parts) > 0 else "N/A",
                "mem_usage": parts[1] if len(parts) > 1 else "N/A",
                "mem_pct": parts[2] if len(parts) > 2 else "N/A",
                "pids": parts[3] if len(parts) > 3 else "N/A",
            }
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
            lines = result.stdout.strip().split("\n")
            workers = [l for l in lines if "office-convert-worker" in l]
            return workers
    except Exception:
        pass
    return []


def convert_file(uploaded_file, progress_placeholder, status_placeholder):
    start_time = time.time()
    files = {"file": (uploaded_file.name, uploaded_file.getvalue(), "application/octet-stream")}
    status_placeholder.info("⏳ Uploading and converting... (no timeout)")

    try:
        response = requests.post(CONVERT_URL, files=files, stream=True, timeout=None)

        if response.status_code != 200:
            elapsed = time.time() - start_time
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text[:500]
            status_placeholder.error(
                f"❌ Failed ({response.status_code}) after {elapsed:.1f}s\n\n```json\n{error_body}\n```"
            )
            return None, elapsed

        chunks = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total_bytes += len(chunk)
            progress_placeholder.text(f"📥 Receiving: {total_bytes / 1024 / 1024:.1f} MB")

        elapsed = time.time() - start_time
        output_data = b"".join(chunks)
        status_placeholder.success(
            f"✅ Done! **{elapsed:.1f}s** | "
            f"Output: {len(output_data) / 1024 / 1024:.2f} MB | "
            f"Input: {len(uploaded_file.getvalue()) / 1024 / 1024:.2f} MB"
        )
        return output_data, elapsed

    except Exception as e:
        elapsed = time.time() - start_time
        status_placeholder.error(f"❌ Error after {elapsed:.1f}s: {e}")
        return None, elapsed


# ============================================================
# TITLE
# ============================================================
st.title("📄 Office Convert — Live Dashboard")


# ============================================================
# LIVE STATS — only this section refreshes every 2 seconds
# ============================================================
@st.fragment(run_every=2)
def live_stats():
    health = get_health()
    stats = get_docker_stats()
    workers = get_worker_processes()

    # Metrics row
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Server", "✅ Ready" if health.get("ready") else "❌ Down")
    col2.metric("CPU", stats["cpu"])
    col3.metric("Memory", stats["mem_pct"])
    col4.metric("Workers", str(len(workers)))
    col5.metric("Jobs", f"{health.get('active_jobs', '?')}/{health.get('max_jobs', '?')}")

    # Details
    st.text(f"Mem: {stats['mem_usage']} | PIDs: {stats['pids']} | License: {health.get('license_days_remaining', '?')} days")

    # Worker processes
    if workers:
        st.markdown(f"**⚙️ {len(workers)} Active Workers:**")
        for w in workers:
            parts = w.split()
            if len(parts) >= 2:
                pid = parts[0]
                cpu = parts[1]
                fmt = "docx" if "docx" in w else "pptx" if "pptx" in w else "xlsx" if "xlsx" in w else "pdf" if "pdf" in w else "?"
                mode = "pool" if "pool" in w else "render" if "render" in w else "probe"
                st.text(f"  ├─ PID {pid} | CPU {cpu}% | {fmt} | {mode}")


live_stats()

st.divider()

# ============================================================
# FILE UPLOAD & CONVERSION (does NOT refresh)
# ============================================================
st.header("🚀 Convert a File")

# Initialize conversion history
if "history" not in st.session_state:
    st.session_state.history = []

uploaded_file = st.file_uploader(
    "Drop a file (DOCX, PPTX, XLSX, PDF, DOC, XLS, PPT) — no size limit",
    type=["docx", "pptx", "xlsx", "pdf", "doc", "xls", "ppt"],
)

if uploaded_file:
    file_size_mb = len(uploaded_file.getvalue()) / 1024 / 1024
    st.info(f"📁 **{uploaded_file.name}** — {file_size_mb:.2f} MB")

    if st.button("▶️ Start Conversion", type="primary"):
        progress_placeholder = st.empty()
        status_placeholder = st.empty()

        output_data, elapsed = convert_file(
            uploaded_file, progress_placeholder, status_placeholder
        )

        if output_data:
            output_name = Path(uploaded_file.name).stem + ".pdf"
            st.session_state.history.insert(0, {
                "name": output_name,
                "input_name": uploaded_file.name,
                "data": output_data,
                "size_mb": len(output_data) / 1024 / 1024,
                "input_size_mb": file_size_mb,
                "elapsed": elapsed,
                "timestamp": time.strftime("%H:%M:%S"),
            })

# Conversion history with download buttons
if st.session_state.history:
    st.divider()
    st.header("📦 Conversion History")
    latest = st.session_state.history[0]
    st.success(
        f"🎉 **Ready!** {latest['input_name']} → {latest['name']} "
        f"({latest['elapsed']:.1f}s, {latest['size_mb']:.1f} MB)"
    )
    for i, item in enumerate(st.session_state.history[:10]):
        col_info, col_dl = st.columns([3, 1])
        with col_info:
            st.text(
                f"[{item['timestamp']}] {item['input_name']} → {item['name']} | "
                f"{item['elapsed']:.1f}s | {item['input_size_mb']:.1f} MB → {item['size_mb']:.1f} MB"
            )
        with col_dl:
            st.download_button(
                label=f"⬇️ {item['name']}",
                data=item["data"],
                file_name=item["name"],
                mime="application/pdf",
                key=f"dl_{i}_{item['timestamp']}",
            )

st.divider()
st.caption("Only stats refresh (every 2s). Upload area stays stable. No timeout. No size limit.")
