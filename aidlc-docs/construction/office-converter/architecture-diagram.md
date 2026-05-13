# Office Convert — Architecture Diagram

## System Overview

```mermaid
flowchart TD
    Client["Client<br/>(Browser / curl / Streamlit UI)"]

    subgraph CONTAINER["Docker Container (4GB RAM + 2GB Swap)"]
        subgraph PYTHON["Python Orchestrator (FastAPI)"]
            Server["server.py<br/>POST /convert"]
            FormatDetect["Format Detection<br/>(Magic bytes + OLE2 streams)"]
            ProbeLite["probe_lite.py<br/>(ZIP metadata / size estimate)"]
            Probe["Full Aspose Probe<br/>(fallback only)"]
            AdaptiveChunk["Adaptive Chunk Planner<br/>(RAM-aware sizing)"]
            Pool["Worker Pool Manager<br/>(persistent processes)"]
            Merge["qpdf Streaming Merge"]
        end

        subgraph WORKERS["C++ Worker Pool (per-format binaries)"]
            W1["worker-docx<br/>(Aspose.Words)"]
            W2["worker-pptx<br/>(Aspose.Slides)"]
            W3["worker-xlsx<br/>(Aspose.Cells)"]
            W4["worker-pdf<br/>(Aspose.PDF)"]
        end
    end

    Client -->|"multipart upload"| Server
    Server --> FormatDetect
    FormatDetect -->|"format detected"| ProbeLite
    ProbeLite -->|"page count (instant)"| AdaptiveChunk
    ProbeLite -.->|"fallback if no metadata"| Probe
    Probe -.->|"page count (slow)"| AdaptiveChunk
    AdaptiveChunk -->|"chunk plan"| Pool
    Pool -->|"load once + render N chunks"| W1
    Pool -->|"load once + render N chunks"| W2
    Pool -->|"load once + render N chunks"| W3
    Pool -->|"load once + render N chunks"| W4
    Pool -->|"re-plan if actual ≠ estimated"| AdaptiveChunk
    W1 -->|"chunk PDFs"| Merge
    W2 -->|"chunk PDFs"| Merge
    W3 -->|"chunk PDFs"| Merge
    W4 -->|"chunk PDFs"| Merge
    Merge -->|"streaming PDF"| Client

    style CONTAINER fill:#f0f0f0,stroke:#333,stroke-width:2px
    style PYTHON fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
    style WORKERS fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
```

## Request Flow (Detailed)

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant D as Format Detect
    participant P as Probe Lite
    participant CP as Chunk Planner
    participant WP as Worker Pool
    participant W as C++ Workers (×4)
    participant Q as qpdf

    C->>S: POST /convert (multipart file)
    S->>D: detect_format(magic_bytes, OLE2 streams)
    D-->>S: format (docx/pptx/xlsx/pdf)

    S->>P: probe_lite(file, format)
    alt OOXML metadata available
        P-->>S: page_count (instant, from ZIP)
    else Metadata stale or missing
        P-->>S: size-based estimate
    end

    S->>CP: adaptive_max_pages(probe, RAM, parallel)
    CP-->>S: chunk plan (N chunks × M pages)

    alt Pool mode (multi-chunk)
        S->>WP: spawn pool workers
        WP->>W: load document (once per worker)
        W-->>WP: actual_page_count

        alt Actual ≠ Estimated
            WP->>CP: re-plan with actual count
            CP-->>WP: corrected chunk plan
        end

        loop For each chunk (parallel)
            WP->>W: render(page_start, page_end)
            W-->>WP: chunk.pdf
        end
    else One-shot mode (single chunk)
        S->>W: render full document
        W-->>S: output.pdf
    end

    S->>Q: concat_streaming(chunk_pdfs)
    Q-->>C: streaming PDF response
```

## Format Detection Flow

```mermaid
flowchart TD
    Input["Uploaded File"]
    Magic["Read Magic Bytes"]

    Input --> Magic

    Magic -->|"PK\\x03\\x04"| ZIP["ZIP → OOXML"]
    Magic -->|"%PDF-"| PDF["PDF format"]
    Magic -->|"\\xD0\\xCF\\x11\\xE0..."| OLE2["OLE2 → Legacy Office"]
    Magic -->|"Other"| Reject["❌ Unsupported"]

    ZIP --> ContentTypes["Read [Content_Types].xml"]
    ContentTypes -->|"wordprocessingml"| DOCX["→ docx"]
    ContentTypes -->|"presentationml"| PPTX["→ pptx"]
    ContentTypes -->|"spreadsheetml"| XLSX["→ xlsx"]

    OLE2 --> StreamScan["Scan 512KB for stream names"]
    StreamScan -->|"WordDocument found"| DOCX2["→ docx"]
    StreamScan -->|"PowerPoint Document"| PPTX2["→ pptx"]
    StreamScan -->|"Workbook / Book"| XLSX2["→ xlsx"]
    StreamScan -->|"No match"| ExtFallback["Filename extension fallback"]

    subgraph RETRY["Format Mismatch Auto-Retry"]
        Worker["Worker rejects file"]
        Hint["Error hints different format"]
        Retry["Retry with correct worker"]
    end

    DOCX2 --> Worker
    XLSX2 --> Worker
    Worker -.->|"'This is a word doc'"| Hint
    Hint --> Retry
```

## Pool Mode vs One-Shot Mode

```mermaid
flowchart LR
    subgraph ONE_SHOT["One-Shot Mode (single chunk)"]
        direction TB
        OS1["Spawn process"]
        OS2["Load document"]
        OS3["Render all pages"]
        OS4["Exit"]
        OS1 --> OS2 --> OS3 --> OS4
    end

    subgraph POOL["Pool Mode (multi-chunk)"]
        direction TB
        P1["Spawn N workers"]
        P2["Load document ONCE per worker"]
        P3["Re-plan from actual page count"]
        P4["Render chunk 1"]
        P5["Render chunk 2"]
        P6["Render chunk N"]
        P7["Quit workers"]
        P1 --> P2 --> P3
        P3 --> P4
        P3 --> P5
        P3 --> P6
        P4 --> P7
        P5 --> P7
        P6 --> P7
    end

    Decision{{"chunks > 1?"}}
    Decision -->|"Yes"| POOL
    Decision -->|"No"| ONE_SHOT
```

## Performance Characteristics

| Metric | Before (v1) | After (optimized) | Improvement |
|--------|-------------|-------------------|-------------|
| Probe time | 15+ min (Aspose) | <0.01s (metadata) | ∞ |
| PPTX 8.5 MB (28 slides) | ~337s | 11.6s | 29× |
| DOCX 42 KB (1 page) | N/A | 0.6s | Baseline |
| XLSX 10 MB (2501 pages) | Timeout | ~10 min | Now completes |
| Repeated conversion | Same | <1s (with cache) | ∞ |

## Key Design Decisions

1. **Pool mode**: Document loaded once, rendered N times → eliminates redundant load overhead
2. **Adaptive chunk sizing**: RAM-aware, parallelism-aware → optimal chunk count per file
3. **Auto re-planning**: Pool reports actual page count → corrects stale estimates automatically
4. **Format retry**: Worker hints at correct format → handles mislabeled files gracefully
5. **Size-based probe fallback**: Instant estimate → avoids 15+ min Aspose probe
6. **Streaming response**: qpdf pipes directly to HTTP → no output buffering
