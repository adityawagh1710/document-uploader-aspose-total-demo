# Frontend Components — unit `html-conversion` (Streamlit UI)

**File**: `office_convert_ui/app.py` (existing single-file Streamlit app; new panel follows the
app's existing section/expander conventions). The UI stays backend-agnostic — it consumes only
the HTTP contract.

## Component hierarchy

```
HTML → PDF · Engine Comparison  (new top-level panel/section)
├── UploadRow
│     st.file_uploader (accept .html/.htm, max 10 MB hint)
├── WaitControlsRow  (applies to Gotenberg run only; inline help explains why)
│     waitDelay        st.text_input  (placeholder "e.g. 2s", validated client-side ≤30s)
│     waitForExpression st.text_input (placeholder "window.status === 'ready'")
├── ActionRow
│     [⚡ Convert with both]   [Gotenberg only]   [Aspose only]
├── ResultsRow (st.columns(2), rendered after conversion)
│     EngineResultCard ×2  (gotenberg | aspose)
│       status badge (✅/❌ + failure_class on error)
│       latency ms · output size · X-Request-ID
│       st.download_button (PDF) · optional inline preview (iframe/base64 embed)
└── LatencyChart
      plotly horizontal bar: one bar per engine, latest run (consistent with existing charts)
```

## State (st.session_state keys, following existing naming conventions)

| Key | Content |
|---|---|
| `html_cmp_results` | dict engine → {status, latency_ms, size_bytes, request_id, pdf_bytes \| error_diagnostic} for the latest run |
| `html_cmp_history` | appended into the EXISTING conversion-history structure (gains `engine` field) |

Cross-service completeness: history/stats panels keep their **API fallback**
(`/v1/conversions` with `engine` field) so conversions fired via curl also appear —
per the established UI-local vs API-wide rule.

## Interactions

1. **Convert with both** (Q7:A): fire both endpoints **in parallel**
   (`concurrent.futures.ThreadPoolExecutor(2)` over `requests.post`), spinner while pending,
   render both cards when both settle. One failing does not hide the other (each card shows its
   own Diagnostic).
2. **Single-engine buttons**: same card rendering, one column populated.
3. Wait fields are sent ONLY to the Gotenberg endpoint. If the user filled wait fields and
   clicks "Aspose only", the UI omits them and shows an info note (mirrors BR-3/D4 server rule
   without triggering a 400).
4. Client-side validation mirrors server rules (size ≤ 10 MB, waitDelay ≤ 30s) for fast
   feedback; the server remains authoritative.

## Existing-surface changes

| Surface | Change |
|---|---|
| Conversion history table | new `engine` column (blank for office conversions) |
| Per-format performance panel | HTML row splits per engine, sourced from `per_engine_html` in `/v1/conversions/stats` |
| Endpoint/API help section | document the two new endpoints + wait fields |

## API integration points

| Component | Endpoint |
|---|---|
| Convert buttons | `POST {API_URL}/v1/convert/html/gotenberg`, `POST {API_URL}/v1/convert/html/aspose` |
| History fallback | `GET {API_URL}/v1/conversions` (reads `engine`) |
| Perf panel | `GET {API_URL}/v1/conversions/stats` (reads `per_engine_html`) |

## Validation/UX rules
- Reject non-.html/.htm uploads client-side with a friendly message.
- Show the JS-fidelity hint prominently: "Gotenberg executes JavaScript; Aspose renders static
  HTML only — differences on dynamic pages are expected and are the point of this comparison."
- Error cards render `failure_class` + detail verbatim from the Diagnostic JSON (operators are
  the audience).
