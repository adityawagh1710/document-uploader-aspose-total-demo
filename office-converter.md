# Chunked Document Conversion with Aspose.Total C++

## Context

Convert arbitrarily-sized Office documents (DOCX, PPTX, XLSX, PDF) to PDF
within a fixed RAM budget per pod, on EKS, using Aspose.Total C++. The
strategy is structural decomposition — split inputs into bounded-size chunks,
convert each chunk in isolation, then concatenate the resulting PDFs.

## Why chunking

Aspose.PDF accumulates the entire output document in an in-memory DOM during
both rendering and merge operations, with a 2–20× amplification factor over
input size. A merge of many chunks via `PdfFileEditor::Concatenate` exhibits
the same behaviour and has been observed (Aspose forum) to consume 20+ GB RAM
on large outputs.

Aspose's input-side memory tuning (`LoadOptions::TempFolder`,
`SaveOptions::TempFolder`, `MemoryOptimization`, `BlobManagementOptions` for
Slides, `MemorySetting::MemoryPreference` for Cells) reduces the amplification
factor but does not bound it. A bounded chunk size, combined with these knobs,
gives a bounded RAM ceiling per worker.

The merge step must therefore use a streaming PDF assembler (qpdf or pdfcpu),
not Aspose. qpdf concatenates PDFs by manipulating object IDs and the xref
table without materialising rendered content, so peak RAM is bounded by the
largest single chunk regardless of final document size.

## Service decomposition

**Aspose service (C++).** Stateless API in front of Aspose.Total C++. Three
operations: probe a document for page count and structural seams, render a
specified page range to PDF, and assemble (split or merge) at the source
format level where Aspose is the right tool. All input and output flows
through file paths on local ephemeral storage; the service does not call S3.

**Orchestrator service (Python or TypeScript).** Owns the workflow: S3 I/O,
chunk planning, parallel dispatch to the Aspose service, PDF assembly via
qpdf, idempotency, and caching. This is where the language choice matters
less than the operational fit with the existing stack.

The split keeps the C++ service small and stateless, lets the orchestrator
scale independently, and isolates the Aspose licence to one pod type.

## End-to-end flow

1. **Ingest.** Orchestrator receives a job referencing an S3 object.
   Computes a content hash for cache keying. Returns immediately if the final
   PDF for that hash already exists in the output bucket.

2. **Download.** Orchestrator pulls the source object to local ephemeral
   storage on its pod. For very large inputs, S3 multipart download with
   parallel ranges keeps wall time low.

3. **Probe.** Orchestrator calls the Aspose service to inspect the document:
   page count, format, structural boundaries (section breaks for Word, sheets
   for Excel, slide ranges for PowerPoint, page ranges for PDF), and an
   estimate of per-page rendering cost based on uncompressed OOXML size and
   embedded media inventory.

4. **Plan chunks.** Orchestrator divides the document into chunks bounded
   by both page count and estimated memory cost. Pathologically heavy regions
   (e.g. a slide with a large embedded video) are isolated into their own
   chunks so the bound holds. Chunk plan is deterministic for a given input.

5. **Dispatch.** Orchestrator uploads the source file to a shared volume or
   issues parallel HTTP calls to the Aspose service, one per chunk, each
   specifying the page range. Concurrency is bounded by the size of the
   Aspose worker pool. Per-chunk cache lookup avoids re-rendering chunks
   whose content hash was seen previously.

6. **Render.** Each Aspose pod loads the source with `LoadOptions::TempFolder`
   set, extracts the requested page range, and renders to a chunk PDF using
   `SaveOptions::TempFolder` and `MemoryOptimization`. The chunk PDF is
   returned to the orchestrator (HTTP response body, or written to a shared
   path).

7. **Stage chunks.** Orchestrator collects all chunk PDFs to local ephemeral
   storage in chunk order. Failed chunks are retried on a worker tier with a
   higher memory ceiling.

8. **Merge.** Orchestrator invokes qpdf (or pdfcpu) to concatenate the chunks
   into the final PDF, streaming the output directly to an S3 multipart upload
   via a pipe. No process holds the full merged document in memory.

9. **Post-process (optional).** Linearise, compress, sign, or convert to PDF/A
   using qpdf or another streaming tool. Each post-processing step retains the
   bounded-memory property.

10. **Cache and emit.** Final PDF is keyed in the output bucket by source hash.
    Job status is reported back to the caller.

## Failure handling

Each chunk is an independent unit of work. A chunk that OOMs its worker is
retried at a higher memory tier; if the largest tier fails, the chunk is
subdivided further (smaller page range) and retried. Only after subdivision
fails does the job dead-letter. Idempotency keys at chunk granularity mean
retries never duplicate completed work.

## Operational notes

Aspose.Total C++ is x86_64-only on Linux. Aspose worker pods must be scheduled
to x86 node groups via taints and tolerations. Orchestrator and qpdf merge
pods can run on any architecture, including ARM64.

Local ephemeral storage on Aspose pods should be sized for both Aspose's
TempFolder spill and the chunk PDFs being produced. Fast local disk (NVMe
instance store, or gp3 with provisioned throughput) materially affects
TempFolder performance under pressure.

Cgroup memory limits should be sized to the worker's tier with swap enabled
as a safety net for the long tail. Chunking is the primary memory bound;
swap is the backstop. Undersized swap is worse than no swap, so generous
sizing matters when the safety net is invoked.

## Component responsibilities

| Component | Owns |
|---|---|
| Aspose service (C++) | Document probing, chunk rendering, format-specific splitting |
| Orchestrator (Py/TS) | S3 I/O, chunk planning, dispatch, retries, caching, qpdf merge |
| qpdf or pdfcpu | Streaming PDF concatenation and post-processing |
| Cache (S3) | Final-document and per-chunk results, keyed by content hash |