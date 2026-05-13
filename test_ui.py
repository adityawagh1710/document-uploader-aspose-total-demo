"""
Office Convert Test UI — Real-time monitoring dashboard.

- Stats refresh every 2s (never freeze, even during conversion)
- Conversion runs in background thread
- Errors displayed properly
- Download history with time taken

Run:
    docker compose --profile ui up -d test-ui
    Open: http://localhost:8501
"""

import os
import subprocess
import time
import threading
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


def do_conversion(file_name, file_bytes):
    """Run conversion. Returns (output_bytes, elapsed, error_msg)."""
    start_time = time.time()
    files = {"file": (file_name, file_bytes, "application/octet-stream")}
    try:
        response = requests.post(CONVERT_URL, files=files, stream=True, timeout=1800)
        elapsed = time.time() - start_time
        if response.status_code != 200:
            try:
                error_body = response.json()
                error_msg = f"Server error ({response.status_code}): {error_body.get('failure_class', 'unknown')} — {error_body.get('detail', {})}"
            except Exception:
                error_msg = f"Server error ({response.status_code}): {response.text[:200]}"
            return None, elapsed, error_msg
        chunks = []
        for chunk in response.iter_content(chunk_size=65536):
            chunks.append(chunk)
        output_data = b"".join(chunks)
        elapsed = time.time() - start_time
        if not output_data or not output_data.startswith(b"%PDF"):
            try:
                error_body = output_data.decode("utf-8", errors="replace")
                if "failure_class" in error_body:
                    return None, elapsed, f"Conversion failed: {error_body[:300]}"
            except Exception:
                pass
            return None, elapsed, "Conversion produced invalid output (not a PDF)"
        return output_data, elapsed, None
    except Exception as e:
        elapsed = time.time() - start_time
        return None, elapsed, f"Error: {e}"


# ============================================================
# TITLE
# ============================================================
st.title("📄 Office Convert — Live Dashboard")


# ============================================================
# LIVE STATS — refreshes every 2s independently
# ============================================================
@st.fragment(run_every=2)
def live_stats():
    health = get_health()
    stats = get_docker_stats()
    workers = get_worker_processes()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Server", "✅ Ready" if health.get("ready") else "❌ Down")
    col2.metric("CPU", stats["cpu"])
    col3.metric("Memory", stats["mem_pct"])
    col4.metric("Workers", str(len(workers)))
    col5.metric("Jobs", f"{health.get('active_jobs', '?')}/{health.get('max_jobs', '?')}")

    st.text(f"Mem: {stats['mem_usage']} | PIDs: {stats['pids']} | License: {health.get('license_days_remaining', '?')} days")

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

    # Show conversion in progress
    if st.session_state.get("converting"):
        elapsed = time.time() - st.session_state.get("convert_start", time.time())
        st.warning(f"⏳ Converting **{st.session_state.get('convert_filename', '')}**... ({elapsed:.0f}s)")


live_stats()

st.divider()

# ============================================================
# FILE UPLOAD & CONVERSION
# ============================================================
@st.fragment
def conversion_section():
    st.header("🚀 Convert a File")

    if "history" not in st.session_state:
        st.session_state.history = []
    if "converting" not in st.session_state:
        st.session_state.converting = False
    if "last_error" not in st.session_state:
        st.session_state.last_error = None

    # Show last error
    if st.session_state.last_error:
        st.error(f"❌ {st.session_state.last_error}")
        if st.button("Dismiss"):
            st.session_state.last_error = None
            st.rerun()

    uploaded_file = st.file_uploader(
        "Drop a file (DOCX, PPTX, XLSX, PDF, DOC, XLS, PPT) — no size limit",
        type=["docx", "pptx", "xlsx", "pdf", "doc", "xls", "ppt"],
    )

    if uploaded_file:
        file_size_mb = len(uploaded_file.getvalue()) / 1024 / 1024
        st.info(f"📁 **{uploaded_file.name}** — {file_size_mb:.2f} MB")

        if st.button("▶️ Start Conversion", type="primary", disabled=st.session_state.converting):
            st.session_state.last_error = None
            st.session_state.converting = True
            st.session_state.convert_filename = uploaded_file.name
            st.session_state.convert_start = time.time()

            # Run in background thread so stats keep updating
            file_name = uploaded_file.name
            file_bytes = uploaded_file.getvalue()
            file_size = file_size_mb

            def _bg_convert():
                data, elapsed, err = do_conversion(file_name, file_bytes)
                st.session_state.converting = False
                if err:
                    st.session_state.last_error = f"{err} (after {elapsed:.1f}s)"
                elif data:
                    output_name = Path(file_name).stem + ".pdf"
                    st.session_state.history.insert(0, {
                        "name": output_name,
                        "input_name": file_name,
                        "data": data,
                        "size_mb": len(data) / 1024 / 1024,
                        "input_size_mb": file_size,
                        "elapsed": elapsed,
                        "timestamp": time.strftime("%H:%M:%S"),
                    })

            thread = threading.Thread(target=_bg_convert, daemon=True)
            thread.start()
            st.rerun()

    if st.session_state.converting:
        elapsed = time.time() - st.session_state.get("convert_start", time.time())
        st.warning(f"⏳ Converting **{st.session_state.get('convert_filename', '')}**... ({elapsed:.0f}s elapsed)")
        if st.button("🔄 Check if done"):
            st.rerun()

    # Conversion history
    if st.session_state.history:
        st.divider()
        st.subheader("📦 Conversion History")
        latest = st.session_state.history[0]
        st.success(
            f"🎉 **Ready!** {latest['input_name']} → {latest['name']} | "
            f"⏱️ **{latest['elapsed']:.1f}s** | "
            f"{latest['input_size_mb']:.1f} MB → {latest['size_mb']:.1f} MB"
        )
        for i, item in enumerate(st.session_state.history[:10]):
            col_info, col_dl = st.columns([3, 1])
            with col_info:
                st.text(
                    f"[{item['timestamp']}] {item['input_name']} → {item['name']} | "
                    f"⏱️ {item['elapsed']:.1f}s | "
                    f"{item['input_size_mb']:.1f} MB → {item['size_mb']:.1f} MB"
                )
            with col_dl:
                st.download_button(
                    label=f"⬇️ {item['name']}",
                    data=item["data"],
                    file_name=item["name"],
                    mime="application/pdf",
                    key=f"dl_{i}_{item['timestamp']}",
                )


conversion_section()

st.divider()
st.caption("Stats refresh every 2s (even during conversion). Click 'Check if done' to see results.")
