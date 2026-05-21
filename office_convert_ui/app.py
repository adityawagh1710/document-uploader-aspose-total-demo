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

import collections
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
CONVERT_URL = f"{API_URL}/v1/convert"
HEALTH_URL = f"{API_URL}/health"  # unversioned by convention (orchestrator probe)
HEARTBEATS_URL = f"{API_URL}/v1/jobs"  # /v1/jobs/{request_id}/heartbeats
PROGRESS_URL = f"{API_URL}/v1/jobs"  # /v1/jobs/{request_id}/progress
TIMINGS_URL = f"{API_URL}/v1/jobs"  # /v1/jobs/{request_id}/timings

# Shared HTTP session for all API calls. Fragments fire every 1-4 seconds
# and each tick makes multiple calls (/health, /stats, /workers,
# /jobs/.../heartbeats, /jobs/.../timings, /jobs/.../progress); without
# a session, every call opens a fresh TCP+TLS connection, adding ~50-200 ms
# of handshake latency per call. urllib3's HTTPAdapter under Session
# reuses the connection pool across calls.
_SESSION = requests.Session()

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

      /* 3-bar equalizer. CSS-only so the Streamlit script never blocks
         (which is what st.spinner did). Negative animation-delay on bars
         2 + 3 staggers them through the cycle so they look out of phase
         from frame zero. Used in two places:
           - Conversion callout (blue, while a job is running)
           - Dashboard header next to LIVE (green, always animating)
         Color is overridden per-context via the --bar-color custom prop. */
      .eq-bars {
        --bar-color: rgba(0,120,255,0.85);
        display: inline-flex;
        align-items: flex-end;
        gap: 2px;
        height: 14px;
        margin-right: 6px;
        vertical-align: -3px;
      }
      .eq-bars span {
        display: inline-block;
        width: 3px;
        background: var(--bar-color);
        border-radius: 1px;
        animation: eq-bar 0.9s ease-in-out infinite;
      }
      .eq-bars span:nth-child(2) { animation-delay: -0.3s; }
      .eq-bars span:nth-child(3) { animation-delay: -0.6s; }
      @keyframes eq-bar {
        0%,100% { height: 3px;  opacity: 0.55; }
        50%     { height: 14px; opacity: 1; }
      }
      /* Header instance: green to match the LIVE semantic + the
         deprecated .live-dot pulse it replaces. */
      .dash-header .eq-bars {
        --bar-color: rgba(34,197,94,0.9);
        margin-right: 4px;
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
      /* Worker-active pulse: when a worker pid shows non-trivial CPU%,
         its row gently pulses cyan to draw the eye to live activity.
         Animate <td> not <tr> — table layout context blocks background
         transitions on <tr> in some browsers. Inset box-shadow on the
         first cell adds a left-accent stripe synchronized to the pulse. */
      @keyframes worker-active-pulse {
        0%,100% { background: rgba(34,211,238,0.04); }
        50%     { background: rgba(34,211,238,0.11); }
      }
      .dash-table tr.worker-active td {
        animation: worker-active-pulse 1.8s ease-in-out infinite;
      }
      .dash-table tr.worker-active td:first-child {
        box-shadow: inset 2px 0 0 rgba(34,211,238,0.75);
      }

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
        /* Prevent mid-word break ("CPU UTILIZATIO" \n "N") at narrow
           viewport widths. Truncate with ellipsis if the card is too
           narrow even for nowrap content. */
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .util-card .value {
        font-size: 36px; font-weight: 700; color: #e2e8f0;
        font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
        line-height: 1;
        /* Prevent breaking "11.8%" at the decimal point — narrow
           viewports were rendering it as "11." \n "8%". Browsers
           treat letter-spaced periods as wrap opportunities; nowrap
           keeps the number atomic. */
        white-space: nowrap;
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
        /* Same nowrap protection as label/value above — the "0% 100%"
           markers were prone to wrap on narrow viewports. */
        white-space: nowrap; overflow: hidden;
      }
      /* Sparkline mini-chart of last N polls. Inline SVG with no fill,
         drawn behind the big number for a Grafana-style trend reveal.
         Width stretches to the card; preserveAspectRatio=none gives us
         that. ~30 samples at 4s cadence = ~2 min of history. */
      .util-card .sparkline {
        display: block;
        width: 100%;
        height: 28px;
        margin-top: -4px;
      }
      .util-card .sparkline polyline {
        fill: none;
        stroke-width: 1.4;
        stroke-linecap: round;
        stroke-linejoin: round;
        vector-effect: non-scaling-stroke;
      }
      .util-card .sparkline.cpu  polyline { stroke: rgba(34,211,238,0.85); }
      .util-card .sparkline.mem  polyline { stroke: rgba(132,204,22,0.85); }
      .util-card .sparkline.warn polyline { stroke: rgba(251,191,36,0.95); }
      .util-card .sparkline.crit polyline { stroke: rgba(248,113,113,0.95); }
      /* Faint area under the line for visual weight */
      .util-card .sparkline polygon {
        opacity: 0.18;
        stroke: none;
      }
      .util-card .sparkline.cpu  polygon { fill: #22d3ee; }
      .util-card .sparkline.mem  polygon { fill: #84cc16; }
      .util-card .sparkline.warn polygon { fill: #fbbf24; }
      .util-card .sparkline.crit polygon { fill: #f87171; }

      /* Empty chart skeleton with shimmer sweep. Replaces the static Plotly
         "Awaiting conversion data" annotation when the chart truly has no
         data — Plotly's canvas can't host a CSS animation, so we render
         a pure-HTML skeleton instead and switch back to Plotly when data
         arrives. Matches the dark gradient + dimensions of the live chart
         so the Mega Row's rhythm stays constant. */
      .chart-skeleton {
        background: linear-gradient(180deg, rgba(30,41,59,0.55), rgba(15,23,42,0.5));
        border: 1px solid rgba(148,163,184,0.12);
        border-radius: 8px;
        padding: 12px 14px;
        height: 210px;
        position: relative;
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }
      .chart-skeleton .title {
        font-size: 12px;
        color: #94a3b8;
        font-weight: 600;
        letter-spacing: 0.01em;
      }
      .chart-skeleton .message {
        font-size: 11px;
        color: rgba(148,163,184,0.7);
        margin: auto;
        z-index: 1;
        position: relative;
      }
      /* Sweep gradient moving left → right (~2.4 s loop). 60% width
         narrow band keeps the effect subtle — Grafana-style "data is
         loading" rather than carnival. */
      .chart-skeleton::before {
        content: "";
        position: absolute;
        top: 0;
        left: -150%;
        width: 60%;
        height: 100%;
        background: linear-gradient(90deg,
          transparent,
          rgba(148,163,184,0.05),
          rgba(148,163,184,0.10),
          rgba(148,163,184,0.05),
          transparent);
        animation: chart-shimmer 2.4s linear infinite;
        pointer-events: none;
      }
      @keyframes chart-shimmer {
        0%   { left: -150%; }
        100% { left: 250%; }
      }

      /* Hover affordances — kpi-tile lifts subtly, pill gets a glow.
         Keeps the interactivity discoverable without being noisy. */
      .kpi-tile {
        transition: transform 180ms cubic-bezier(0.4, 0, 0.2, 1),
                    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
                    box-shadow   180ms cubic-bezier(0.4, 0, 0.2, 1);
      }
      .kpi-tile:hover {
        transform: translateY(-1px);
        border-color: rgba(148,163,184,0.30);
        box-shadow: 0 6px 18px rgba(0,0,0,0.28);
      }
      .pill {
        transition: box-shadow 200ms ease, filter 200ms ease;
      }
      .pill:hover {
        box-shadow: 0 0 10px currentColor;
        filter: brightness(1.12);
      }

      /* Mini progress bar inside a KPI tile — used by the License tile
         to visualize the days-remaining ratio. Color tracks the tile's
         status class (ok / warn / crit) for consistency. */
      .kpi-tile .tile-bar {
        height: 4px;
        background: rgba(148,163,184,0.12);
        border-radius: 2px;
        overflow: hidden;
        margin-top: 6px;
      }
      .kpi-tile .tile-bar-fill {
        height: 100%;
        border-radius: 2px;
        transition: width 600ms cubic-bezier(0.4, 0, 0.2, 1);
      }
      .kpi-tile.ok   .tile-bar-fill { background: linear-gradient(90deg, #22c55e, #4ade80); }
      .kpi-tile.warn .tile-bar-fill { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
      .kpi-tile.crit .tile-bar-fill { background: linear-gradient(90deg, #ef4444, #f87171); }
      .kpi-tile.info .tile-bar-fill { background: linear-gradient(90deg, #0ea5e9, #38bdf8); }
      .kpi-tile.dim  .tile-bar-fill { background: linear-gradient(90deg, #64748b, #94a3b8); }

      /* Per-format performance summary panel — 4 cells (docx/pptx/xlsx/pdf),
         each showing count + avg + p95. Color-coded left border matches
         the format icon emoji's tonal vibe so it reads at a glance. */
      .format-perf {
        background: linear-gradient(180deg, rgba(30,41,59,0.55), rgba(15,23,42,0.5));
        border: 1px solid rgba(148,163,184,0.12);
        border-radius: 8px;
        padding: 10px 14px 12px 14px;
        margin-top: 10px;
      }
      .format-perf .title {
        font-size: 10.5px;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
        margin-bottom: 8px;
        display: flex;
        justify-content: space-between;
        align-items: baseline;
      }
      .format-perf .title .total {
        color: #64748b;
        font-weight: 400;
        text-transform: none;
        letter-spacing: 0;
        font-size: 11px;
      }
      .format-perf .grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 10px;
      }
      .format-perf .cell {
        background: rgba(148,163,184,0.06);
        border: 1px solid rgba(148,163,184,0.08);
        border-left-width: 3px;
        border-radius: 5px;
        padding: 8px 10px;
        transition: background 180ms ease, border-color 180ms ease;
      }
      .format-perf .cell:hover {
        background: rgba(148,163,184,0.10);
      }
      .format-perf .cell.docx { border-left-color: #38bdf8; }
      .format-perf .cell.pptx { border-left-color: #f87171; }
      .format-perf .cell.xlsx { border-left-color: #4ade80; }
      .format-perf .cell.pdf  { border-left-color: #fbbf24; }
      .format-perf .cell.dim  { border-left-color: rgba(148,163,184,0.3); opacity: 0.55; }
      .format-perf .cell .row1 {
        display: flex;
        align-items: baseline;
        gap: 6px;
      }
      .format-perf .cell .icon { font-size: 15px; line-height: 1; }
      .format-perf .cell .fmt  { font-size: 9.5px; color: #94a3b8; letter-spacing: 0.06em; font-weight: 700; text-transform: uppercase; }
      .format-perf .cell .count { font-size: 20px; font-weight: 700; color: #e2e8f0; font-variant-numeric: tabular-nums; line-height: 1.1; margin-left: auto; }
      .format-perf .cell .stats { font-size: 10.5px; color: #94a3b8; font-variant-numeric: tabular-nums; margin-top: 4px; display: flex; justify-content: space-between; }
      .format-perf .cell .stats .label { color: #64748b; letter-spacing: 0.04em; }

      /* Toast notification (success/error on conversion completion).
         position:fixed pins to viewport edge regardless of where the
         element ends up in the DOM. translateX(110%) starts off-screen
         right; slide-in then fade-out animation runs once via `forwards`
         to leave the toast invisible past 5 s. */
      .toast-container {
        position: fixed;
        top: 16px;
        right: 16px;
        z-index: 9999;
        pointer-events: none;
      }
      .toast {
        background: linear-gradient(180deg, rgba(30,41,59,0.95), rgba(15,23,42,0.92));
        border: 1px solid rgba(148,163,184,0.22);
        border-left-width: 4px;
        border-radius: 6px;
        padding: 10px 14px;
        min-width: 260px;
        max-width: 360px;
        font-size: 12.5px;
        color: #e2e8f0;
        box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        backdrop-filter: blur(8px);
        animation: toast-life 5s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      }
      .toast .toast-title {
        font-weight: 600;
        font-size: 13px;
        margin-bottom: 2px;
        font-variant-numeric: tabular-nums;
      }
      .toast .toast-body {
        color: #94a3b8;
        font-variant-numeric: tabular-nums;
      }
      .toast.ok  { border-left-color: rgba(34,197,94,0.85); }
      .toast.ok  .toast-title { color: #4ade80; }
      .toast.err { border-left-color: rgba(239,68,68,0.85); }
      .toast.err .toast-title { color: #f87171; }
      @keyframes toast-life {
        0%   { transform: translateX(110%); opacity: 0; }
        8%   { transform: translateX(0);    opacity: 1; }
        85%  { transform: translateX(0);    opacity: 1; }
        100% { transform: translateX(110%); opacity: 0; }
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
def _render_tile(
    label: str,
    value: str,
    status: str = "info",
    sub: str = "",
    bar_pct: float | None = None,
) -> str:
    """KPI tile: label + big value + optional sub-text + optional mini bar.

    `bar_pct` (0..100) adds a slim progress bar below the sub line — used by
    the License tile to visualize days-remaining/365. Bar color matches the
    tile's `status` class via the .kpi-tile.{ok,warn,crit,info,dim} CSS
    selectors defined in the page-level <style> block.
    """
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    if bar_pct is not None:
        p = max(0.0, min(100.0, float(bar_pct)))
        bar_html = (
            f'<div class="tile-bar">'
            f'<div class="tile-bar-fill" style="width:{p:.1f}%"></div>'
            f'</div>'
        )
    else:
        bar_html = ""
    return (
        f'<div class="kpi-tile {status}">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{value}</div>'
        f"{sub_html}"
        f"{bar_html}"
        f"</div>"
    )


def _format_key(filename: str) -> str:
    """Canonical format key for stat aggregation. Maps OOXML + legacy
    binary extensions onto the 4 worker buckets, with `other` for the
    edge case where the file's extension doesn't match any known set.
    Kept in lock-step with _format_icon's branches."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in {"docx", "doc", "dot", "dotx", "dotm", "docm"}:
        return "docx"
    if ext in {"pptx", "ppt", "pot", "pps", "ppsx", "potx", "pptm"}:
        return "pptx"
    if ext in {"xlsx", "xls", "xlsm", "xlt", "xltx", "xlsb"}:
        return "xlsx"
    if ext == "pdf":
        return "pdf"
    return "other"


_FORMAT_ICONS = {"docx": "📄", "pptx": "📊", "xlsx": "📈", "pdf": "📕", "other": "📦"}


def _format_icon(filename: str) -> str:
    """Emoji per Office format, derived from filename extension."""
    return _FORMAT_ICONS[_format_key(filename)]


def _percentile(values: list[float], pct: float) -> float:
    """Cheap nearest-rank percentile. For len ≤ 1 returns the value /
    0.0; otherwise sorts (in-place on a copy) and indexes at floor(N*pct)
    clamped to len-1. Good enough for the 4-cell perf panel — no scipy."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * pct), len(s) - 1)
    return s[idx]


def _human_bytes(n: float) -> str:
    """Compact bytes-to-string for the Lifetime tile sub-text. Switches
    units at the 1024 boundary so "1.2 GB" stays under 8 chars even at
    multi-TB scale."""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TB"


def _render_format_perf(stats: dict) -> str:
    """4-cell perf panel (docx/pptx/xlsx/pdf): icon + count + avg + p95.

    `stats` is `_state()["per_format_stats"]` — a dict keyed by format
    with `count` (int) + `times` (deque[float] of recent wall-times).
    Cells with `count==0` render as dimmed placeholders so the row
    layout stays stable from cold start through populated use.
    """
    cells_html = []
    total = 0
    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        bucket = stats.get(fmt, {"count": 0, "times": []})
        count = bucket.get("count", 0)
        times = list(bucket.get("times") or [])
        total += count
        if count == 0:
            cells_html.append(
                f'<div class="cell {fmt} dim">'
                f'<div class="row1">'
                f'<span class="icon">{_FORMAT_ICONS[fmt]}</span>'
                f'<span class="fmt">{fmt}</span>'
                f'<span class="count">—</span>'
                f'</div>'
                f'<div class="stats">'
                f'<span><span class="label">avg</span> —</span>'
                f'<span><span class="label">p95</span> —</span>'
                f'</div>'
                f'</div>'
            )
        else:
            avg = sum(times) / len(times) if times else 0.0
            p95 = _percentile(times, 0.95)
            cells_html.append(
                f'<div class="cell {fmt}">'
                f'<div class="row1">'
                f'<span class="icon">{_FORMAT_ICONS[fmt]}</span>'
                f'<span class="fmt">{fmt}</span>'
                f'<span class="count">{count:,}</span>'
                f'</div>'
                f'<div class="stats">'
                f'<span><span class="label">avg</span> {avg:.1f}s</span>'
                f'<span><span class="label">p95</span> {p95:.1f}s</span>'
                f'</div>'
                f'</div>'
            )
    return (
        '<div class="format-perf">'
        '<div class="title">Per-format performance'
        f'<span class="total">{total:,} total</span>'
        '</div>'
        '<div class="grid">'
        f'{"".join(cells_html)}'
        '</div>'
        '</div>'
    )


def _render_chart_skeleton(title: str, message: str = "Awaiting conversion data") -> str:
    """HTML skeleton (with CSS shimmer sweep) shown in place of a Plotly
    figure when the chart has no data. Plotly's canvas can't host a CSS
    animation, so we switch render mode entirely until data arrives."""
    return (
        f'<div class="chart-skeleton">'
        f'<div class="title">{title}</div>'
        f'<div class="message">{message}</div>'
        f'</div>'
    )


def _render_tile_row(tiles: list[str], *, stacked: bool = False) -> str:
    """Render 5 KPI tiles as a flex row (default) or a vertical stack.

    The horizontal `tile-row` is the default now — Row 1 of the dashboard
    spans the full width with all 5 tiles inline. `stacked=True` packs the
    tiles vertically for narrow side columns (kept for future layouts)."""
    cls = "tile-stack" if stacked else "tile-row"
    return f'<div class="{cls}">' + "".join(tiles) + "</div>"


# Process-wide ring buffers of recent metric samples. Maintained at module
# scope so they survive Streamlit's per-rerun script execution. Each `live_stats`
# fragment tick appends one sample. 30 samples × ~4 s cadence = ~2 min of trend
# in the sparkline.
_METRIC_HIST_LEN = 30
_metric_hist: dict[str, collections.deque[float]] = {
    "cpu": collections.deque(maxlen=_METRIC_HIST_LEN),
    "mem": collections.deque(maxlen=_METRIC_HIST_LEN),
}


def _record_metric(name: str, value: float) -> None:
    buf = _metric_hist.get(name)
    if buf is not None:
        buf.append(float(value))


def _render_sparkline(values: list[float], css_class: str) -> str:
    """Inline SVG sparkline. Y-axis pinned to 0..100 because both CPU% and
    Mem% live in that range — pinning means a flatlined gauge stays
    visually flat instead of auto-scaling to noise. polyline = line,
    polygon = faint shaded area below it for a small visual weight bump."""
    if len(values) < 2:
        return ""
    width = 100  # arbitrary viewBox width; CSS stretches to card width
    height = 28
    n = len(values)
    x_step = width / (n - 1)
    pts = [
        (i * x_step, height - max(0.0, min(100.0, v)) / 100.0 * height)
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{height} {line} {width},{height}"
    return (
        f'<svg class="sparkline {css_class}" '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f'<polygon points="{area}" />'
        f'<polyline points="{line}" />'
        f'</svg>'
    )


def _render_util_card(label: str, pct: float, base_class: str = "cpu", sub: str = "") -> str:
    """Big number + colored progress bar + sparkline of recent samples.

    Bar colour shifts to warn / crit when utilisation crosses 70 / 90 %.
    Sparkline below the bar shows ~last 2 min of history (30 samples at
    ~4 s cadence). Y-axis pinned to 0..100 so a flatline reads flat.
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
    spark_html = _render_sparkline(list(_metric_hist.get(base_class, [])), fill_class)
    return (
        f'<div class="util-card">'
        f'<div class="label">{label}</div>'
        f'<div class="value">{p:.1f}%</div>'
        f'<div class="bar"><div class="bar-fill {fill_class}" style="width:{p}%"></div></div>'
        f'{sub_html}'
        f'{spark_html}'
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
        # Cumulative since process start (NOT bounded by MAX_RECENT_RESULTS).
        # Drives the Lifetime KPI tile.
        "total_conversions": 0,
        "total_input_bytes": 0.0,
        "total_output_bytes": 0.0,
        # Per-format performance stats. Each value is a dict with `count`
        # (lifetime) and `times` (bounded deque of last 100 wall-times in
        # seconds for p95/p50 percentile rendering). Drives the per-format
        # performance summary panel under the Mega Row.
        "per_format_stats": {
            fmt: {"count": 0, "times": collections.deque(maxlen=100)}
            for fmt in ("docx", "pptx", "xlsx", "pdf", "other")
        },
    }


def get_health():
    try:
        return _SESSION.get(HEALTH_URL, timeout=3).json()
    except Exception as e:
        return {"error": str(e)}


_DEFAULT_DOCKER_STATS = {"cpu": "N/A", "mem_usage": "N/A", "mem_pct": "N/A", "pids": "N/A"}


@st.cache_resource
def _docker_monitor() -> dict:
    """Background-thread cache of container stats from the API's /stats + /workers.

    Replaces the original `docker stats` / `docker top` subprocess path so the
    same UI works on:
      - Docker compose locally (cgroup v1)
      - EKS dev05 pods (cgroup v2; no docker socket, but /sys/fs/cgroup works)
      - any other container runtime that exposes cgroup files (always the case)

    The API exposes raw cumulative counters via GET /stats and GET /workers
    (see office_convert/container_stats.py). This loop fetches both ~every
    second and computes CPU% from the delta between consecutive samples.
    """
    state: dict = {
        "lock": threading.Lock(),
        "stats": dict(_DEFAULT_DOCKER_STATS),
        "workers": [],
    }

    def _fmt_bytes_mib(n: int) -> str:
        return f"{n / 1024 / 1024:.2f}MiB"

    def _refresh_loop() -> None:
        # Cumulative-counter state for CPU% delta computation.
        prev_cpu_usec: int | None = None
        prev_at: float | None = None
        prev_workers: dict[int, tuple[int, float]] = {}  # pid -> (cpu_usec, sampled_at)

        while True:
            new_stats = dict(_DEFAULT_DOCKER_STATS)
            try:
                r = _SESSION.get(f"{API_URL}/v1/stats", timeout=2)
                if r.ok:
                    s = r.json()
                    cur_usec = int(s["cpu_usage_usec"])
                    cur_at = float(s["sampled_at"])
                    if prev_cpu_usec is not None and prev_at is not None and cur_at > prev_at:
                        d_usec = max(0, cur_usec - prev_cpu_usec)
                        d_t = cur_at - prev_at
                        cpu_pct = (d_usec / 1_000_000.0) / d_t * 100.0
                    else:
                        cpu_pct = 0.0
                    prev_cpu_usec = cur_usec
                    prev_at = cur_at

                    mem_bytes = int(s["mem_bytes"])
                    mem_max = int(s["mem_max_bytes"])
                    mem_pct = (mem_bytes / mem_max * 100.0) if mem_max > 0 else 0.0
                    mem_usage_str = (
                        f"{_fmt_bytes_mib(mem_bytes)} / {_fmt_bytes_mib(mem_max)}"
                        if mem_max > 0
                        else _fmt_bytes_mib(mem_bytes)
                    )
                    new_stats = {
                        "cpu": f"{cpu_pct:.2f}%",
                        "mem_usage": mem_usage_str,
                        "mem_pct": f"{mem_pct:.2f}%",
                        "pids": str(int(s["pids_current"])),
                    }
            except Exception:
                pass

            new_workers: list[dict] = []
            try:
                r = _SESSION.get(f"{API_URL}/v1/workers", timeout=2)
                if r.ok:
                    fresh_workers = r.json().get("workers", [])
                    current_pids: set[int] = set()
                    for w in fresh_workers:
                        pid = int(w["pid"])
                        cur_usec = int(w["cpu_usage_usec"])
                        cur_at = float(w["sampled_at"])
                        current_pids.add(pid)
                        prev = prev_workers.get(pid)
                        if prev is not None and cur_at > prev[1]:
                            d_usec = max(0, cur_usec - prev[0])
                            d_t = cur_at - prev[1]
                            cpu_pct_w = (d_usec / 1_000_000.0) / d_t * 100.0
                        else:
                            cpu_pct_w = 0.0
                        prev_workers[pid] = (cur_usec, cur_at)
                        new_workers.append(
                            {
                                "pid": pid,
                                "cpu_pct": cpu_pct_w,
                                "rss_bytes": int(w.get("rss_bytes", 0)),
                                "cmdline": w.get("cmdline", ""),
                                "etime_sec": float(w.get("etime_sec", 0.0)),
                            }
                        )
                    # Drop sample state for PIDs that have exited so the dict
                    # doesn't grow unbounded across a long Streamlit session.
                    for stale_pid in list(prev_workers.keys()):
                        if stale_pid not in current_pids:
                            del prev_workers[stale_pid]
            except Exception:
                pass

            with state["lock"]:
                state["stats"] = new_stats
                state["workers"] = new_workers

            time.sleep(1.0)

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
        resp = _SESSION.get(f"{HEARTBEATS_URL}/{request_id}/heartbeats", timeout=2)
        if resp.status_code != 200:
            return []
        return resp.json().get("heartbeats", []) or []
    except Exception:
        return []


def get_timings(request_id: str) -> list[dict]:
    """Fetch the stage-timing trail (load / paginate / save events per worker)."""
    try:
        resp = _SESSION.get(f"{TIMINGS_URL}/{request_id}/timings", timeout=2)
        if resp.status_code != 200:
            return []
        return resp.json().get("timings", []) or []
    except Exception:
        return []


def get_progress(request_id: str) -> dict:
    """Fetch weighted progress for an in-flight request."""
    try:
        resp = _SESSION.get(f"{PROGRESS_URL}/{request_id}/progress", timeout=2)
        if resp.status_code != 200:
            return {}
        return resp.json() or {}
    except Exception:
        return {}


def _format_diagnostic(status_code: int, body: dict) -> str:
    """Render the server's JSON Diagnostic as a single human-readable line.

    Server emits {failure_class, request_id, detail: {...}}. `failure_class`
    alone is a slug ("unsupported_format", "render_failed"); the actionable
    text — the Aspose exception, the rejected ODF subtype, the size ceiling
    — lives in `detail`. Pick the first useful field and tack it onto the
    failure class so the user sees *why* in the UI without reading logs.
    """
    failure_class = body.get("failure_class", "unknown")
    detail = body.get("detail") or {}
    text = (
        detail.get("reason")
        or detail.get("message")
        or (detail.get("stderr_tail") or "").strip()
    )
    if not text:
        if "size_bytes" in detail and "ceiling_bytes" in detail:
            text = f"{detail['size_bytes']} > {detail['ceiling_bytes']} byte limit"
        elif "retry_after_seconds" in detail:
            text = f"retry after {detail['retry_after_seconds']}s"
        elif "expired_on" in detail and detail["expired_on"]:
            text = f"expired on {detail['expired_on']}"
    if text:
        # Truncate runaway stderr tails so the Streamlit toast stays readable.
        if len(text) > 240:
            text = text[:237] + "..."
        return f"Error {status_code} ({failure_class}): {text}"
    return f"Error {status_code}: {failure_class}"


def do_conversion(file_name, file_bytes, request_id):
    """Blocking conversion. Returns (data, elapsed, error)."""
    start = time.time()
    try:
        resp = _SESSION.post(
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
                    _format_diagnostic(resp.status_code, body),
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
            # Preserved so the Re-run button can rerun the SAME bytes
            # without the user re-uploading. Migrates to the result entry
            # on completion (only the latest result holds them — older
            # entries get them popped to keep memory bounded).
            "input_bytes": file_bytes,
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
            # Migrate input bytes from s["active"] to the result entry for
            # one-click Re-run, but ONLY on the new latest result — strip
            # them from older entries first to bound memory (inputs can
            # be up to 1 GiB per OFFICE_CONVERT_MAX_INPUT_BYTES).
            input_bytes_for_rerun = s["active"].get("input_bytes")
            for old in s["results"]:
                old.pop("input_data", None)
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
                    "input_data": input_bytes_for_rerun,
                },
            )
            del s["results"][MAX_RECENT_RESULTS:]
            # Lifetime counters survive the rolling history cap above.
            s["total_conversions"] = s.get("total_conversions", 0) + 1
            s["total_input_bytes"] = s.get("total_input_bytes", 0.0) + input_size_mb * 1024 * 1024
            s["total_output_bytes"] = s.get("total_output_bytes", 0.0) + len(data)
            # Per-format aggregate for the perf summary panel.
            _fk = _format_key(input_name)
            _bucket = s.setdefault("per_format_stats", {}).setdefault(
                _fk, {"count": 0, "times": collections.deque(maxlen=100)}
            )
            _bucket["count"] += 1
            _bucket["times"].append(float(elapsed))
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

# Bound session-state memory growth: each history item holds the full output
# PDF bytes (`item["data"]`) and a Streamlit download_button blob, so an
# unbounded list quickly pushes the UI pod past its 1.5Gi limit. Cap to the
# same ceiling the process-wide store uses.
del st.session_state.history[MAX_RECENT_RESULTS:]

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
    '<span class="eq-bars" aria-label="Live"><span></span><span></span><span></span></span>LIVE</span>'
    '</div>',
    unsafe_allow_html=True,
)

# Floating slot for the conversion-complete toast. position:fixed in CSS
# means the DOM location is irrelevant; we put the slot up top so it
# renders early in the page lifecycle.
_slot_toast = st.empty()

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

# Full-width per-format performance summary panel (docx/pptx/xlsx/pdf).
# Lives just under the Mega Row so it pairs visually with the Lifetime
# tile while taking its own row's vertical space for legibility.
_slot_format_perf = st.empty()


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

    # Lifetime counters + per-format aggregates live in the process-wide
    # state dict (survive the rolling MAX_RECENT_RESULTS cap on s["results"]).
    # Snapshot under the lock then drop it — the renderer doesn't need it.
    _s = _state()
    with _s["lock"]:
        total_conv = _s.get("total_conversions", 0)
        total_in_bytes = _s.get("total_input_bytes", 0.0)
        total_out_bytes = _s.get("total_output_bytes", 0.0)
        per_fmt_snapshot = {
            fmt: {
                "count": bucket.get("count", 0),
                "times": list(bucket.get("times") or []),
            }
            for fmt, bucket in (_s.get("per_format_stats") or {}).items()
        }

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
            # 365 d == 100% bar; clamped in _render_tile. Visualizes the
            # countdown so the days-remaining number isn't the only signal.
            bar_pct=(license_days / 365.0) * 100.0,
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
        _render_tile(
            "Lifetime",
            f"{total_conv:,}",
            "info" if total_conv > 0 else "dim",
            sub=f"{_human_bytes(total_in_bytes)} in · {_human_bytes(total_out_bytes)} out",
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
    _record_metric("cpu", cpu_pct)
    _record_metric("mem", mem_pct)

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
            pid = w.get("pid", "")
            cpu_val = float(w.get("cpu_pct", 0.0))
            cmdline = w.get("cmdline", "")
            fmt = (
                "docx" if "docx" in cmdline
                else "pptx" if "pptx" in cmdline
                else "xlsx" if "xlsx" in cmdline
                else "pdf" if "pdf" in cmdline
                else "—"
            )
            mode = (
                "pool" if "pool" in cmdline
                else "render" if "render" in cmdline
                else "probe"
            )
            cpu_pct_w = max(0.0, min(100.0, cpu_val))
            # Threshold > 0.5% avoids pulsing on float-rounding noise from
            # workers that report 0.1% jitter when idle.
            row_class = "worker-active" if cpu_val > 0.5 else ""
            rows.append(
                f'<tr class="{row_class}">'
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

    # Per-format performance summary — rendered after the Mega Row so the
    # tile/util-card density stays compact above and this larger 4-cell
    # panel anchors the section below.
    _slot_format_perf.markdown(
        _render_format_perf(per_fmt_snapshot), unsafe_allow_html=True
    )


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
def _build_empty_chart(title: str, message: str = "Awaiting conversion data") -> go.Figure:
    """Empty styled placeholder chart for the "no data yet" state.

    Matches the dark grid + dimensions of the live charts so the Mega Row's
    rhythm is stable from page-load (pre-first-conversion) through live runs.
    Centered annotation reads as "instrument ready" rather than "feature
    missing". `message` overrides the default text — used by the timing /
    Gantt slots to call out that those charts only populate for XLSX inputs
    today (worker_cpp/formats/{docx,pptx,pdf}.cpp don't yet emit timing
    events; only xlsx.cpp does).
    """
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(size=11, color="rgba(148,163,184,0.7)"),
    )
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=title, font=dict(size=12)),
        xaxis=dict(
            title=None,
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            zerolinecolor="rgba(255,255,255,0.18)",
            tickfont=dict(size=9),
            showticklabels=False,
            range=[0, 1],
        ),
        yaxis=dict(
            title=None,
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            zerolinecolor="rgba(255,255,255,0.18)",
            tickfont=dict(size=9),
            showticklabels=False,
            range=[0, 1],
        ),
        height=210,
        margin=dict(l=8, r=14, t=36, b=24),
        plot_bgcolor="rgba(15,23,42,0.7)",
        paper_bgcolor="rgba(15,23,42,0)",
        showlegend=False,
    )
    return fig


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
def toast_renderer():
    """Slide-in toast in the top-right when a conversion completes.

    State machine:
      - `toast_shown_ids` (session) tracks result ids we've already toasted
        so the toast doesn't re-fire on every fragment tick after completion.
      - `toast_active` (session) holds the in-flight toast + an `expires_at`
        wall-clock so we can clear the slot after the 5 s CSS animation.

    The CSS animation runs once on the slot's first render (Streamlit's diff
    won't re-render identical HTML on subsequent ticks), then we explicitly
    `.empty()` the slot at expiry to clean up the DOM.
    """
    now = time.time()
    pending = st.session_state.get("toast_active")
    if pending is not None and now >= pending.get("expires_at", 0):
        _slot_toast.empty()
        st.session_state["toast_active"] = None
        pending = None

    if pending is None:
        # Look for a brand-new completion to toast.
        _active, results, err = _snapshot()
        shown = st.session_state.setdefault("toast_shown_ids", set())
        candidate = None
        if results:
            r = results[0]
            if r["id"] not in shown:
                candidate = ("ok", r["id"], r["name"], r.get("time", 0.0), r.get("out_mb", 0.0))
        if candidate is None and err:
            err_id = f"err_{err.get('ts')}"
            if err_id not in shown:
                candidate = ("err", err_id, err.get("msg", "Conversion failed"), 0.0, 0.0)
        if candidate is None:
            return
        kind, cid, title, secs, out_mb = candidate
        shown.add(cid)
        if kind == "ok":
            html = (
                '<div class="toast-container"><div class="toast ok">'
                f'<div class="toast-title">✓ {title}</div>'
                f'<div class="toast-body">'
                f'{secs:.1f}s &middot; {out_mb:.2f} MB output'
                f'</div></div></div>'
            )
        else:
            html = (
                '<div class="toast-container"><div class="toast err">'
                f'<div class="toast-title">✖ Conversion failed</div>'
                f'<div class="toast-body">{title}</div>'
                f'</div></div>'
            )
        _slot_toast.markdown(html, unsafe_allow_html=True)
        st.session_state["toast_active"] = {"id": cid, "expires_at": now + 5.0}


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
            f'<span class="eq-bars" aria-label="Converting">'
            f'<span></span><span></span><span></span></span>'
            f'Converting <b>{active["input_name"]}</b> '
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
    "Drop a file (DOCX, PPTX, XLSX, PDF, DOC, XLS, PPT, CSV, RTF, ODT, ODS, ODP, "
    "ODG — ODF/ODB upload but are rejected: no rendering library for them)",
    # ODG goes through the LibreOffice fallback path (Aspose.Total C++
    # can't render drawing pages). ODF/ODB are still accepted at the
    # picker so the server's per-subtype rejection message reaches the UI
    # toast instead of Streamlit's generic "files of type X are not
    # allowed" — see _format_diagnostic.
    type=[
        "docx", "pptx", "xlsx", "pdf",
        "doc", "xls", "ppt",
        "csv", "rtf",
        "odt", "ods", "odp", "odg",
        "odf", "odb",
    ],
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
            rid = None
            label = (
                '<div style="font-size:0.85rem;opacity:0.6;margin-top:0.6rem;'
                'margin-bottom:-0.3rem;">📊 Awaiting first conversion · '
                'charts will populate live</div>'
            )
        else:
            label = (
                f'<div style="font-size:0.85rem;opacity:0.7;margin-top:0.6rem;'
                f'margin-bottom:-0.3rem;">📊 Last completed · '
                f'<code style="font-size:0.8rem;">{rid}</code> · '
                f'{last["input"]} · {last["time"]:.1f}s · '
                f'<span style="opacity:0.6;">data kept for 30 min</span></div>'
            )
    else:
        rid = None
        label = (
            '<div style="font-size:0.85rem;opacity:0.6;margin-top:0.6rem;'
            'margin-bottom:-0.3rem;">📊 Awaiting first conversion · '
            'charts will populate live</div>'
        )

    _slot_chart_label.markdown(label, unsafe_allow_html=True)

    # Empty-state message for the timing + Gantt slots. All four format
    # workers (docx/pptx/pdf/xlsx) now emit `pool_load.*` + `pool_render.*`
    # timing events via the shared worker_cpp/timing_util.h helper, so the
    # only reason these charts stay empty is "no in-flight or recent run"
    # — same as the Memory chart.
    timing_msg = "Awaiting timing data"

    # For each chart: real Plotly figure when data exists, shimmer-animated
    # HTML skeleton when empty. The skeleton replaces the Plotly empty-state
    # because Plotly's canvas can't host a CSS animation.
    fig_mem = _build_memory_chart(rid) if rid else None
    if fig_mem is None:
        _slot_chart_mem.markdown(
            _render_chart_skeleton("💾 Memory over time"),
            unsafe_allow_html=True,
        )
    else:
        fig_mem.update_layout(uirevision="mem-chart")
        _slot_chart_mem.plotly_chart(
            fig_mem,
            width="stretch",
            key="chart_mem",
            config={"displayModeBar": False},
        )

    fig_tim = _build_timing_chart(rid) if rid else None
    if fig_tim is None:
        _slot_chart_tim.markdown(
            _render_chart_skeleton("⏱️ Time per stage", message=timing_msg),
            unsafe_allow_html=True,
        )
    else:
        fig_tim.update_layout(uirevision="tim-chart")
        _slot_chart_tim.plotly_chart(
            fig_tim,
            width="stretch",
            key="chart_tim",
            config={"displayModeBar": False},
        )

    fig_gantt = _build_chunk_gantt(rid) if rid else None
    if fig_gantt is None:
        _slot_chart_gantt.markdown(
            _render_chart_skeleton("📊 Chunk Gantt", message=timing_msg),
            unsafe_allow_html=True,
        )
    else:
        fig_gantt.update_layout(uirevision="gantt-chart")
        _slot_chart_gantt.plotly_chart(
            fig_gantt,
            width="stretch",
            key="chart_gantt",
            config={"displayModeBar": False},
        )


# Toast watcher — fires the slide-in notification when a conversion
# completes. Always runs (regardless of whether one is in flight) so it
# can catch completions that happen between page renders.
toast_renderer()

# Stats display — drives the slots that live in the top section. Called
# from here so the fragment definitions earlier in the file are in scope.
# live_charts() handles three states internally:
#   - active conversion → real-time data
#   - completed results → last conversion's recap
#   - neither → 3 "Awaiting conversion data" skeletons
# Without this unconditional call, a fresh container start (no history yet)
# leaves the Mega Row right side blank — defeats the skeleton design from
# commit 3db61fa.
if _snap_active is not None:
    conversion_status()
live_charts()

# Action block — only the upload/start UI lives below the file picker.
# Stats display above is independent of upload state.
if uploaded_file:
    if _snap_active is not None:
        st.warning(
            "⏳ A conversion is already running — submit another after it finishes."
        )
    else:
        # `.size` reads the size from Streamlit's UploadedFile metadata
        # without materializing the bytes. `getvalue()` would copy the full
        # buffer just to call len() on it — wasted O(file_size) work on
        # every script rerun until the user clicks Start Conversion.
        size_mb = uploaded_file.size / 1024 / 1024
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
    _hdr_col, _clear_col, _cache_col = st.columns([5, 1, 1], vertical_alignment="center")
    _hdr_col.header("📦 Conversion History")
    # "Clear all" is broader than the per-row 🗑️: it wipes both this
    # session's view AND the process-wide s["results"] store, so other
    # browser tabs / refreshes also see an empty history. Toast tracking
    # is reset too, otherwise re-converted files wouldn't re-toast.
    if _clear_col.button(
        "🧹 Clear all",
        key="clear_history_all",
        help="Wipe this session's history AND the server-side history store",
        use_container_width=True,
    ):
        s = _state()
        with s["lock"]:
            s["results"] = []
            s["last_error"] = None
        st.session_state.history = []
        st.session_state.seen_result_ids = set()
        st.session_state.toast_shown_ids = set()
        st.session_state.toast_active = None
        st.rerun()
    # "Clear cache" hits the API's DELETE /cache endpoint to wipe the
    # on-disk conversion cache (chunks + final PDFs). Independent of
    # the history wipe above. No-op success on EKS because the chart
    # doesn't set OFFICE_CONVERT_CACHE_DIR — the API returns
    # {"enabled": False} and we report that as "Cache is disabled".
    if _cache_col.button(
        "🗑️ Clear cache",
        key="clear_server_cache",
        help="Wipe the API's on-disk conversion cache (chunks + final PDFs)",
        use_container_width=True,
    ):
        try:
            resp = _SESSION.delete(f"{API_URL}/v1/cache", timeout=10).json()
            if not resp.get("enabled"):
                st.toast("Cache is disabled on this deployment.", icon="ℹ️")
            else:
                freed_mb = resp.get("bytes_freed", 0) / 1024 / 1024
                st.toast(
                    f"Cleared {resp.get('files_deleted', 0)} files "
                    f"({freed_mb:.1f} MB freed)",
                    icon="✅",
                )
        except Exception as e:
            st.toast(f"Cache clear failed: {e}", icon="❌")

    # Filter input — case-insensitive substring on the input filename.
    # `latest` (the green success banner) and the per-row list both derive
    # from the filtered view; if no matches, we show an info line instead
    # of the banner so users don't see "latest" entries that don't match.
    filter_term = st.text_input(
        "Filter history",
        value=st.session_state.get("history_filter", ""),
        placeholder="🔍 Filter by filename (e.g. 'report', '.xlsx')…",
        key="history_filter",
        label_visibility="collapsed",
    )
    needle = (filter_term or "").strip().lower()
    if needle:
        filtered = [h for h in st.session_state.history if needle in h.get("input", "").lower()]
        st.caption(
            f"Showing **{len(filtered)}** of **{len(st.session_state.history)}** entries · "
            f"filter: `{filter_term}`"
        )
    else:
        filtered = st.session_state.history

    if not filtered:
        st.info(f"No history entries match `{filter_term}`.")
        latest = None
    else:
        latest = filtered[0]
        # Two-column row: success banner on the left, optional Re-run button
        # on the right. Re-run is only enabled when the input bytes were
        # preserved (true for the most recent entry; older entries had their
        # input_data popped to bound memory).
        _msg_col, _rerun_col = st.columns([5, 1], vertical_alignment="center")
        with _msg_col:
            st.success(
                f"{_format_icon(latest['input'])} **{latest['input']}** → {latest['name']} | "
                f"⏱️ **{latest['time']:.1f}s** | {latest['out_mb']:.1f} MB"
            )
        with _rerun_col:
            if latest.get("input_data"):
                if st.button(
                    "🔄 Re-run",
                    key="rerun_latest",
                    help="Re-convert the same input file with no re-upload",
                    use_container_width=True,
                ):
                    ok = _start_conversion(latest["input"], latest["input_data"])
                    if not ok:
                        st.warning("A conversion is already running.")
                    st.rerun()

    for i, item in enumerate(filtered[:10]):
        col1, col2, col3 = st.columns([4, 1, 1])
        with col1:
            st.markdown(
                f"{_format_icon(item['input'])} **{item['input']}** → `{item['name']}`  \n"
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
            # Per-session remove: drops just this entry from
            # st.session_state.history. The process-wide s["results"] store
            # (capped at MAX_RECENT_RESULTS) is untouched, so other sessions
            # / full page refreshes still see the entry until process rotation
            # ages it out. seen_result_ids retains the id so the deleted
            # entry doesn't reappear on the next script rerun in THIS session.
            del_id = item.get("id")
            if st.button(
                "🗑️",
                key=f"del_{del_id or i}_{item['ts']}",
                help="Remove from this session's history",
            ):
                st.session_state.history = [
                    h for h in st.session_state.history if h.get("id") != del_id
                ]
                st.rerun()

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
