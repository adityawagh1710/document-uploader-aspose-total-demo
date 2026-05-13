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
#include <Aspose.Cells/PdfSaveOptions.h>
#include <Aspose.Cells/SaveFormat.h>
#include <Aspose.Cells/U16String.h>
#include <Aspose.Cells/Vector.h>
#include <Aspose.Cells/Workbook.h>
#include <Aspose.Cells/WorkbookRender.h>
#include <Aspose.Cells/WorksheetCollection.h>

#include <cstdio>
#include <cstdint>
#include <cstdlib>
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

void render_xlsx(const RenderArgs& args) {
    Aspose::Cells::Workbook wb(to_u16(args.input).c_str());
    Aspose::Cells::PdfSaveOptions opts;
    configure_natural_pagination(opts);
    // RenderArgs page_start/page_end are 1-based and inclusive; Cells's
    // PageIndex is 0-based and PageCount is a length. The orchestrator
    // already clamps page_end to probe.page_count, so this slice is in-range.
    opts.SetPageIndex(args.page_start - 1);
    opts.SetPageCount(args.page_end - args.page_start + 1);
    wb.Save(to_u16(args.output).c_str(), opts);
}

int probe_xlsx_page_count(const std::string& input) {
    Aspose::Cells::Workbook wb(to_u16(input).c_str());
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
// XLSX pool stores the input path and reloads per render (Workbook is
// mutated by Save with PageIndex/PageCount in some Cells versions).
// The load cost for Cells is the pagination pass, which we do once in
// pool_load to get the page count; each pool_render still pays a
// Workbook.Load but skips the pagination (PageIndex/PageCount are set
// directly).

namespace {
std::string g_xlsx_input_path;
int g_xlsx_page_count = 0;
}  // namespace

int pool_load(const std::string& input_path) {
    g_xlsx_input_path = input_path;
    g_xlsx_page_count = probe_xlsx_page_count(input_path);
    return g_xlsx_page_count;
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (g_xlsx_input_path.empty()) {
        throw RenderException("pool_render: no document loaded");
    }
    Aspose::Cells::Workbook wb(to_u16(g_xlsx_input_path).c_str());
    Aspose::Cells::PdfSaveOptions opts;
    configure_natural_pagination(opts);
    opts.SetPageIndex(page_start - 1);
    opts.SetPageCount(page_end - page_start + 1);
    wb.Save(to_u16(output_path).c_str(), opts);
}

}  // namespace office_convert
