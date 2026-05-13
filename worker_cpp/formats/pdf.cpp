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

#include <cstdio>
#include <string>

#include "../error.h"
#include "../license.h"
#include "../pool.h"
#include "../probe.h"
#include "../probe_util.h"
#include "../render.h"

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
    g_page_count = probe_pdf_page_count(input_path);
    return g_page_count;
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (g_input_path.empty()) {
        throw RenderException("pool_render: no document loaded");
    }
    render_pdf_range(g_input_path, page_start, page_end, output_path);
}

}  // namespace office_convert
