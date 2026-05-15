// xlsx.cpp — Aspose.Cells-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-xlsx binary.
//
// Cells uses a plain-C++ API (no CodePorting framework). The 4-binary split
// is the load-bearing reason XLSX now works: in the old single-binary
// design, the Slides 26.4 / Words 26.3 CodePorting framework was loaded
// into the same process and wedged Cells's plain-C++ Workbook constructor.
// With this TU's binary linking only libAspose.Cells.so, no CodePorting
// .so enters the process address space.
//
// Page-range subsetting: PaginatedSaveOptions::SetPageIndex/SetPageCount
// (zero-based) — RenderArgs is 1-based, so subtract 1 on the way in.
// Probe page count comes from WorkbookRender::GetPageCount() (full
// pagination pass, no rasterization). Probe and render share the same
// PageSetup-driven pagination, so page indices align.

#include <Aspose.Cells/CellsException.h>
#include <Aspose.Cells/ImageOrPrintOptions.h>
#include <Aspose.Cells/Initializer.h>
#include <Aspose.Cells/License.h>
#include <Aspose.Cells/LoadOptions.h>
#include <Aspose.Cells/MemorySetting.h>
#include <Aspose.Cells/PdfOptimizationType.h>
#include <Aspose.Cells/PdfSaveOptions.h>
#include <Aspose.Cells/SaveFormat.h>
#include <Aspose.Cells/U16String.h>
#include <Aspose.Cells/Vector.h>
#include <Aspose.Cells/Workbook.h>
#include <Aspose.Cells/WorkbookRender.h>
#include <Aspose.Cells/WorksheetCollection.h>

#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "../error.h"
#include "../license.h"
#include "../pool.h"
#include "../probe.h"
#include "../probe_util.h"
#include "../render.h"

namespace office_convert {

namespace {

constexpr const char* kFormat = "xlsx";

// atexit-compatible Cells library shutdown. Aspose.Cells must be Cleanup()-d
// before process exit so OpenSSL (used for license RSA-signature verification)
// is torn down cleanly. The atexit hook is registered exactly once, in
// apply_license — the worker is short-lived (one chunk per spawn) so there
// are no nesting concerns.
extern "C" void aspose_cells_cleanup_atexit() {
    Aspose::Cells::Cleanup(true);
}

// ASCII std::string → std::u16string for Cells's char16_t* API.
// Production paths (license, scratch, S3 keys) are ASCII.
std::u16string to_u16(const std::string& s) {
    std::u16string out;
    out.reserve(s.size());
    for (unsigned char c : s) {
        out.push_back(static_cast<char16_t>(c));
    }
    return out;
}

// UTF-16 → UTF-8 (BMP-only) for diagnostic messages. Caller MUST bind the
// U16String to a named local before calling — passing a temporary leaves
// GetData() pointing at a destroyed object.
std::string narrow(const Aspose::Cells::U16String& s) {
    const char16_t* data = s.GetData();
    if (data == nullptr) {
        return {};
    }
    std::string out;
    for (; *data != 0; ++data) {
        unsigned int cp = static_cast<unsigned int>(*data);
        if (cp < 0x80) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }
    return out;
}

// Emit one timing event to stderr as a single JSON line. The Python stderr
// reader recognizes {"type":"timing", ...} and forwards it as a structured
// `pool_worker_timing` log event so we can see where the per-chunk time
// actually goes (load vs pagination vs save). Single write to keep the line
// atomic against interleaved heartbeats from the worker's other stderr
// emitters.
inline void emit_timing_ms(const char* stage,
                            std::chrono::steady_clock::duration d) {
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(d).count();
    std::cerr << "{\"type\":\"timing\",\"stage\":\"" << stage
              << "\",\"duration_ms\":" << ms << "}\n" << std::flush;
}

// Both probe and render must agree on what counts as "a page", or the
// orchestrator will slice ranges the renderer never produces.
//
// Policy: vertical pagination preserved (OnePagePerSheet=false) — must
// stay false, otherwise a 1M-row sheet flattens into a single page that
// silently drops most rows (Aspose 26.4 behavior verified on
// sample_sales_data.xlsx). Horizontal slicing suppressed
// (AllColumnsInOnePagePerSheet=true) — without this, wide sheets like
// dashboards-with-charts get their columns chopped across PDF pages
// (verified on student_marks_with_charts.xlsx: the Total column and
// both charts were sliced vertically across pages 1 and 2). Forcing
// all columns onto one page width scales them down to fit but never
// drops data — safe across the full XLSX size range, from small
// dashboards to multi-million-row corpora.
//
// Probe and render share these settings via this helper so PageIndex/
// PageCount in the render-side slice maps to the same pages the probe
// counted.
void configure_natural_pagination(Aspose::Cells::PaginatedSaveOptions& opts) {
    opts.SetOnePagePerSheet(false);
    opts.SetAllColumnsInOnePagePerSheet(true);
}
void configure_natural_pagination(Aspose::Cells::Rendering::ImageOrPrintOptions& opts) {
    opts.SetOnePagePerSheet(false);
    opts.SetAllColumnsInOnePagePerSheet(true);
}

// Optimized LoadOptions for PDF-render-only workloads. High-aesthetics
// files (charts, conditional formatting, decorative shapes, formulas)
// pay enormous load cost under default settings. Since we only render
// to PDF (never save back to XLSX), skip formula parsing, useless
// shapes, and unparsed data; use compact memory representation.
Aspose::Cells::LoadOptions make_render_load_options() {
    Aspose::Cells::LoadOptions opts(Aspose::Cells::LoadFormat::Xlsx);
    opts.SetMemorySetting(Aspose::Cells::MemorySetting::MemoryPreference);
    opts.SetParsingFormulaOnOpen(false);
    opts.SetIgnoreUselessShapes(true);
    opts.SetKeepUnparsedData(false);
    return opts;
}

void render_xlsx(const RenderArgs& args) {
    auto load_opts = make_render_load_options();
    Aspose::Cells::Workbook wb(to_u16(args.input).c_str(), load_opts);
    Aspose::Cells::PdfSaveOptions opts;
    configure_natural_pagination(opts);
    // Render-side speed optimizations for high-aesthetics files:
    // - MinimumSize skips full font embedding for ASCII 32-127 and
    //   optimizes border rendering — significantly faster on styled sheets.
    // - Resample images to 150 PPI / 80% JPEG — charts and embedded images
    //   are the dominant per-page render cost; 150 PPI is screen-quality
    //   and cuts rasterization time dramatically vs the default 220+ PPI.
    // - Skip font compatibility checks (we control the font environment).
    opts.SetOptimizationType(Aspose::Cells::Rendering::PdfOptimizationType::MinimumSize);
    opts.SetImageResample(150, 80);
    opts.SetCheckFontCompatibility(false);
    opts.SetEmbedStandardWindowsFonts(false);
    // RenderArgs page_start/page_end are 1-based and inclusive; Cells's
    // PageIndex is 0-based and PageCount is a length. The orchestrator
    // already clamps page_end to probe.page_count, so this slice is in-range.
    opts.SetPageIndex(args.page_start - 1);
    opts.SetPageCount(args.page_end - args.page_start + 1);
    wb.Save(to_u16(args.output).c_str(), opts);
}

int probe_xlsx_page_count(const std::string& input) {
    auto load_opts = make_render_load_options();
    Aspose::Cells::Workbook wb(to_u16(input).c_str(), load_opts);
    // Full pagination, no rasterization — drives the chunk planner.
    // Pagination is bounded by per-sheet PageSetup + the natural-print
    // flags configured here. Matches the render-side path so PageIndex/
    // PageCount align.
    Aspose::Cells::Rendering::ImageOrPrintOptions opts;
    configure_natural_pagination(opts);
    Aspose::Cells::Rendering::WorkbookRender wr(wb, opts);
    return wr.GetPageCount();
}

}  // namespace

void apply_license(const std::string& license_path) {
    // Pre-check file readability so a missing/unreadable license fails cleanly
    // before we engage any Aspose state.
    {
        FILE* f = std::fopen(license_path.c_str(), "rb");
        if (f == nullptr) {
            throw LicenseException("license file not readable: " + license_path);
        }
        std::fclose(f);
    }
    try {
        // Aspose.Cells/Initializer.h: "This method must be invoked before
        // using the library and only needs to be invoked once." Skipping
        // Startup() empirically produces ExceptionType::Internal (code=24,
        // message="encoding") from SetLicense — most likely because
        // Aspose.Cells's license validator uses OpenSSL (ASN.1 / RSA
        // signature verification) and OpenSSL is initialized inside
        // Startup(). The other three Aspose products (Words, Slides, PDF)
        // have no equivalent init contract, which is why the same umbrella
        // license unlocked them. The Aspose-shipped example main.cpp
        // (vendor/aspose/Cells/example/src/main.cpp) calls Startup() before
        // any License operations.
        Aspose::Cells::Startup();
        std::atexit(aspose_cells_cleanup_atexit);

        Aspose::Cells::License license;
        license.SetLicense(to_u16(license_path).c_str());
    } catch (const Aspose::Cells::CellsException& e) {
        const Aspose::Cells::U16String err = e.GetErrorMessage();
        throw LicenseException(std::string("Aspose::Cells SetLicense (code=") +
                               std::to_string(static_cast<int>(e.GetCode())) +
                               "): " + narrow(err));
    } catch (const WorkerError&) {
        throw;
    } catch (const std::exception& e) {
        throw LicenseException(std::string("Aspose::Cells SetLicense: ") + e.what());
    }
}

void dispatch_render(const RenderArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-xlsx invoked with format=" + args.format);
    }
    try {
        render_xlsx(args);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("XLSX render bad_alloc");
    } catch (const Aspose::Cells::CellsException& e) {
        // CellsException does not derive from std::exception — catch explicitly.
        const Aspose::Cells::U16String err = e.GetErrorMessage();
        throw RenderException(std::string("XLSX (CellsException code=") +
                              std::to_string(static_cast<int>(e.GetCode())) +
                              "): " + narrow(err));
    } catch (const std::exception& e) {
        throw RenderException(std::string("XLSX: ") + e.what());
    }
}

void dispatch_probe(const ProbeArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-xlsx invoked with format=" + args.format);
    }
    int page_count = 0;
    try {
        page_count = probe_xlsx_page_count(args.input);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("probe bad_alloc");
    } catch (const Aspose::Cells::CellsException& e) {
        const Aspose::Cells::U16String err = e.GetErrorMessage();
        throw InputUnprocessableException(std::string("probe (CellsException code=") +
                                          std::to_string(static_cast<int>(e.GetCode())) +
                                          "): " + narrow(err));
    } catch (const std::exception& e) {
        throw InputUnprocessableException(std::string("probe: ") + e.what());
    }
    emit_probe_json(page_count, kFormat, file_size_bytes(args.input));
}

// --- Pool mode ---
// XLSX pool stores the input path and reloads per render. The held-Workbook
// variant (initial fix #1 attempt 2026-05-15) was reverted after observation
// on sample_large.xlsx (2.65 MB, 800 pages) showed memory growing linearly
// during render — held Workbook + per-Save render-state accumulation pushed
// the container toward the 4 GB cgroup cap, and the resulting swap pressure
// erased any load-cost savings (the real bottleneck for this file class is
// per-page render compute, not load).
//
// Reload-per-chunk costs ~13 s + ~11 s pagination per chunk per worker; with
// pool_size=4 each worker handles only one chunk, so total setup overhead is
// ~24 s/worker regardless of chunk count — acceptable when the render itself
// dominates.

namespace {
std::string g_xlsx_input_path;
int g_xlsx_page_count = 0;
}  // namespace

int pool_load(const std::string& input_path) {
    g_xlsx_input_path = input_path;

    // Inlined from probe_xlsx_page_count so we can split the timings into
    // (workbook_load) and (pagination). Separate stages tell us which one
    // dominates per-chunk cost — load alone, vs the GetPageCount() walk
    // that paginates the entire workbook.
    auto t0 = std::chrono::steady_clock::now();
    auto load_opts = make_render_load_options();
    Aspose::Cells::Workbook wb(to_u16(input_path).c_str(), load_opts);
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.workbook_load", t1 - t0);

    Aspose::Cells::Rendering::ImageOrPrintOptions opts;
    configure_natural_pagination(opts);
    Aspose::Cells::Rendering::WorkbookRender wr(wb, opts);

    auto t2 = std::chrono::steady_clock::now();
    int page_count = wr.GetPageCount();
    auto t3 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.pagination", t3 - t2);

    g_xlsx_page_count = page_count;
    return page_count;
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (g_xlsx_input_path.empty()) {
        throw RenderException("pool_render: no document loaded");
    }

    // 1. Workbook reload (each chunk currently pays this — instrument so we
    //    can quantify exactly how much of the per-chunk wall-time this is).
    auto t0 = std::chrono::steady_clock::now();
    auto load_opts = make_render_load_options();
    Aspose::Cells::Workbook wb(to_u16(g_xlsx_input_path).c_str(), load_opts);
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.workbook_load", t1 - t0);

    Aspose::Cells::PdfSaveOptions opts;
    configure_natural_pagination(opts);
    opts.SetPageIndex(page_start - 1);
    opts.SetPageCount(page_end - page_start + 1);

    // Render-side speed optimizations (same as render_xlsx one-shot path)
    opts.SetOptimizationType(Aspose::Cells::Rendering::PdfOptimizationType::MinimumSize);
    opts.SetImageResample(150, 80);
    opts.SetCheckFontCompatibility(false);
    opts.SetEmbedStandardWindowsFonts(false);

    // 2. The actual render — Save() walks the configured page range and
    //    writes a PDF. Suspected to be the dominant cost. Emit total +
    //    per-page so we can see if some chunks have hotter pages than
    //    others.
    auto t2 = std::chrono::steady_clock::now();
    wb.Save(to_u16(output_path).c_str(), opts);
    auto t3 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.save", t3 - t2);

    const int pages_in_chunk = page_end - page_start + 1;
    const long save_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t3 - t2).count();
    const long per_page_ms = pages_in_chunk > 0 ? save_ms / pages_in_chunk : 0L;
    std::cerr << "{\"type\":\"timing\",\"stage\":\"pool_render.summary\""
              << ",\"pages\":" << pages_in_chunk
              << ",\"page_start\":" << page_start
              << ",\"page_end\":" << page_end
              << ",\"save_ms\":" << save_ms
              << ",\"per_page_ms\":" << per_page_ms
              << "}\n" << std::flush;
}

}  // namespace office_convert
