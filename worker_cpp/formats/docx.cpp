// docx.cpp — Aspose.Words-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-docx binary.
//
// Page-range subsetting via PdfSaveOptions::set_PageSet with explicit
// zero-based page indices.

#include <Aspose.Words.Cpp/Document.h>
#include <Aspose.Words.Cpp/Licensing/License.h>
#include <Aspose.Words.Cpp/Saving/PageSet.h>
#include <Aspose.Words.Cpp/Saving/PdfSaveOptions.h>
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

constexpr const char* kFormat = "docx";

// Module-level document handle for pool mode.
System::SharedPtr<Aspose::Words::Document> g_document;

void verify_license_file(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (f == nullptr) {
        throw LicenseException("license file not readable: " + path);
    }
    std::fclose(f);
}

void render_docx_from(System::SharedPtr<Aspose::Words::Document> doc,
                      int page_start, int page_end,
                      const std::string& output) {
    const int count = page_end - page_start + 1;
    auto pages = System::MakeArray<int32_t>(count);
    for (int i = 0; i < count; ++i) {
        pages[i] = page_start - 1 + i;  // 0-based indices
    }
    auto page_set = System::MakeObject<Aspose::Words::Saving::PageSet>(pages);

    auto opts = System::MakeObject<Aspose::Words::Saving::PdfSaveOptions>();
    opts->set_PageSet(page_set);
    opts->set_TempFolder(System::String("/tmp"));
    opts->set_MemoryOptimization(true);

    doc->Save(System::String(output.c_str()), opts);
}

void render_docx(const RenderArgs& args) {
    auto doc = System::MakeObject<Aspose::Words::Document>(
        System::String(args.input.c_str()));
    render_docx_from(doc, args.page_start, args.page_end, args.output);
}

int probe_docx_page_count(const std::string& input) {
    auto doc = System::MakeObject<Aspose::Words::Document>(
        System::String(input.c_str()));
    return doc->get_PageCount();
}

}  // namespace

void apply_license(const std::string& license_path) {
    verify_license_file(license_path);
    try {
        auto license = System::MakeObject<Aspose::Words::License>();
        license->SetLicense(System::String(license_path.c_str()));
    } catch (const std::exception& e) {
        throw LicenseException(std::string("Aspose::Words SetLicense: ") + e.what());
    }
}

void dispatch_render(const RenderArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-docx invoked with format=" + args.format);
    }
    try {
        render_docx(args);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("DOCX render bad_alloc");
    } catch (const std::exception& e) {
        throw RenderException(std::string("DOCX: ") + e.what());
    }
}

void dispatch_probe(const ProbeArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-docx invoked with format=" + args.format);
    }
    int page_count = 0;
    try {
        page_count = probe_docx_page_count(args.input);
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

int pool_load(const std::string& input_path) {
    g_document = System::MakeObject<Aspose::Words::Document>(
        System::String(input_path.c_str()));
    return g_document->get_PageCount();
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (!g_document) {
        throw RenderException("pool_render: no document loaded");
    }
    render_docx_from(g_document, page_start, page_end, output_path);
}

}  // namespace office_convert
