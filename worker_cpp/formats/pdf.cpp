// pdf.cpp — Aspose.PDF-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-pdf binary.
//
// Page-range subsetting: delete pages outside the requested range from
// a copy of the loaded document, then save. PageCollection::Add(Page) is
// private in v26.4, so we use Delete() instead.

#include <Aspose.PDF.Cpp/Document.h>
#include <Aspose.PDF.Cpp/Page.h>
#include <Aspose.PDF.Cpp/PageCollection.h>
#include <Aspose.PDF.Cpp/PdfLicense.h>
#include <system/array.h>
#include <system/object.h>
#include <system/string.h>

#include <chrono>
#include <cstdio>
#include <iostream>
#include <string>

#include "../error.h"
#include "../license.h"
#include "../pool.h"
#include "../probe.h"
#include "../probe_util.h"
#include "../render.h"
#include "../timing_util.h"

namespace office_convert {

namespace {

constexpr const char* kFormat = "pdf";

// Module-level: store the input path for pool mode (PDF needs to reload
// per render because Delete() mutates the document).
std::string g_input_path;
int g_page_count = 0;

void verify_license_file(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (f == nullptr) {
        throw LicenseException("license file not readable: " + path);
    }
    std::fclose(f);
}

void render_pdf_range(const std::string& input, int page_start, int page_end,
                      const std::string& output) {
    auto source = System::MakeObject<Aspose::Pdf::Document>(
        System::String(input.c_str()));

    int total_pages = source->get_Pages()->get_Count();

    // Fast path: full document
    if (page_start == 1 && page_end >= total_pages) {
        source->Save(System::String(output.c_str()));
        return;
    }

    // Delete pages after the requested range (from end backwards)
    for (int i = total_pages; i > page_end; --i) {
        source->get_Pages()->Delete(i);
    }
    // Delete pages before the requested range
    for (int i = 1; i < page_start; ++i) {
        source->get_Pages()->Delete(1);
    }

    source->Save(System::String(output.c_str()));
}

void render_pdf(const RenderArgs& args) {
    render_pdf_range(args.input, args.page_start, args.page_end, args.output);
}

int probe_pdf_page_count(const std::string& input) {
    auto doc = System::MakeObject<Aspose::Pdf::Document>(
        System::String(input.c_str()));
    return doc->get_Pages()->get_Count();
}

}  // namespace

void apply_license(const std::string& license_path) {
    verify_license_file(license_path);
    try {
        auto license = System::MakeObject<Aspose::Pdf::License>();
        license->SetLicense(System::String(license_path.c_str()));
    } catch (const std::exception& e) {
        throw LicenseException(std::string("Aspose::Pdf SetLicense: ") + e.what());
    }
}

void dispatch_render(const RenderArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-pdf invoked with format=" + args.format);
    }
    try {
        render_pdf(args);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("PDF render bad_alloc");
    } catch (const std::exception& e) {
        throw RenderException(std::string("PDF: ") + e.what());
    }
}

void dispatch_probe(const ProbeArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-pdf invoked with format=" + args.format);
    }
    int page_count = 0;
    try {
        page_count = probe_pdf_page_count(args.input);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("probe bad_alloc");
    } catch (const std::exception& e) {
        throw InputUnprocessableException(std::string("probe: ") + e.what());
    }
    emit_probe_json(page_count, kFormat, file_size_bytes(args.input));
}

// --- Pool mode ---
// PDF pool_render reloads from disk each time because Delete() mutates
// the Document. The load is cheap for PDF (just xref parsing, no layout).

int pool_load(const std::string& input_path) {
    g_input_path = input_path;

    auto t0 = std::chrono::steady_clock::now();
    g_page_count = probe_pdf_page_count(input_path);
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.probe", t1 - t0);

    return g_page_count;
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (g_input_path.empty()) {
        throw RenderException("pool_render: no document loaded");
    }

    // Reload the document each render (PDF pool reloads because Delete()
    // mutates state). Instrument load / mutate / save separately so the
    // Time-per-stage chart shows where per-chunk cost actually goes.
    auto t0 = std::chrono::steady_clock::now();
    auto source = System::MakeObject<Aspose::Pdf::Document>(
        System::String(g_input_path.c_str()));
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.document_load", t1 - t0);

    int total_pages = source->get_Pages()->get_Count();

    auto t2 = std::chrono::steady_clock::now();
    // Fast path: full document — skip the Delete() loops entirely.
    if (page_start == 1 && page_end >= total_pages) {
        // no-op; just measure the (empty) mutation phase
    } else {
        // Delete pages after the requested range (from end backwards)
        for (int i = total_pages; i > page_end; --i) {
            source->get_Pages()->Delete(i);
        }
        // Delete pages before the requested range
        for (int i = 1; i < page_start; ++i) {
            source->get_Pages()->Delete(1);
        }
    }
    auto t3 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.delete_pages", t3 - t2);

    auto t4 = std::chrono::steady_clock::now();
    source->Save(System::String(output_path.c_str()));
    auto t5 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.save", t5 - t4);

    const int pages_in_chunk = page_end - page_start + 1;
    const long save_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t5 - t4).count();
    emit_render_summary(pages_in_chunk, page_start, page_end, save_ms);
}

}  // namespace office_convert
