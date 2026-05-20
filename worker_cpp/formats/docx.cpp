// docx.cpp — Aspose.Words-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-docx binary.
//
// Page-range subsetting via PdfSaveOptions::set_PageSet with explicit
// zero-based page indices.

#include <Aspose.Words.Cpp/Document.h>
#include <Aspose.Words.Cpp/Licensing/License.h>
#include <Aspose.Words.Cpp/LoadFormat.h>
#include <Aspose.Words.Cpp/Loading/DocumentLoadingArgs.h>
#include <Aspose.Words.Cpp/Loading/IDocumentLoadingCallback.h>
#include <Aspose.Words.Cpp/Loading/LoadOptions.h>
#include <Aspose.Words.Cpp/Saving/PageSet.h>
#include <Aspose.Words.Cpp/Saving/PdfSaveOptions.h>
#include <system/array.h>
#include <system/object.h>
#include <system/string.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <sstream>
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

constexpr const char* kFormat = "docx";

// Module-level document handle for pool mode.
System::SharedPtr<Aspose::Words::Document> g_document;

// Aspose.Words' LoadFormat::Auto fails to detect ODT in this C++ build —
// the loader expects OOXML structure when handed a zip-magic file and raises
// FileCorruptedException on the ODF layout. Force the LoadFormat::Odt hint
// when the input filename advertises ODF Text (.odt) or its template (.ott).
bool input_ends_with_odt(const std::string& path) {
    auto ends = [&](const char* suffix) {
        size_t n = std::strlen(suffix);
        return path.size() >= n
            && path.compare(path.size() - n, n, suffix) == 0;
    };
    return ends(".odt") || ends(".ott");
}

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
    auto load_opts = System::MakeObject<Aspose::Words::Loading::LoadOptions>();
    if (input_ends_with_odt(args.input)) {
        load_opts->set_LoadFormat(Aspose::Words::LoadFormat::Odt);
    }
    auto doc = System::MakeObject<Aspose::Words::Document>(
        System::String(args.input.c_str()), load_opts);
    render_docx_from(doc, args.page_start, args.page_end, args.output);
}

int probe_docx_page_count(const std::string& input) {
    auto load_opts = System::MakeObject<Aspose::Words::Loading::LoadOptions>();
    if (input_ends_with_odt(input)) {
        load_opts->set_LoadFormat(Aspose::Words::LoadFormat::Odt);
    }
    auto doc = System::MakeObject<Aspose::Words::Document>(
        System::String(input.c_str()), load_opts);
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

namespace {

// Aspose.Words load-progress callback. Fires throughout the Document
// constructor as the SDK parses the OOXML zip + WordML tree. EstimatedProgress
// returns 0.0..1.0. We throttle to integer-percent boundaries so a 100 MB
// load doesn't emit thousands of near-identical lines.
class LoadProgressCallback : public Aspose::Words::Loading::IDocumentLoadingCallback {
public:
    void Notify(System::SharedPtr<Aspose::Words::Loading::DocumentLoadingArgs> args) override {
        const double p = args->get_EstimatedProgress();
        const int pct = static_cast<int>(p * 100.0);
        if (pct == last_emitted_pct_) return;
        last_emitted_pct_ = pct;
        std::ostringstream oss;
        oss << "{\"type\":\"load_progress\",\"pool_index\":" << current_pool_index()
            << ",\"value\":" << (pct / 100.0) << "}\n";
        std::cerr << oss.str() << std::flush;
    }
private:
    int last_emitted_pct_ = -1;
};

}  // namespace

int pool_load(const std::string& input_path) {
    auto t0 = std::chrono::steady_clock::now();
    auto load_opts = System::MakeObject<Aspose::Words::Loading::LoadOptions>();
    auto cb = System::MakeObject<LoadProgressCallback>();
    load_opts->set_ProgressCallback(cb);
    if (input_ends_with_odt(input_path)) {
        load_opts->set_LoadFormat(Aspose::Words::LoadFormat::Odt);
    }
    g_document = System::MakeObject<Aspose::Words::Document>(
        System::String(input_path.c_str()), load_opts);
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.document_load", t1 - t0);

    auto t2 = std::chrono::steady_clock::now();
    int page_count = g_document->get_PageCount();
    auto t3 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.pagination", t3 - t2);

    return page_count;
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (!g_document) {
        throw RenderException("pool_render: no document loaded");
    }

    auto t0 = std::chrono::steady_clock::now();
    render_docx_from(g_document, page_start, page_end, output_path);
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.save", t1 - t0);

    const int pages_in_chunk = page_end - page_start + 1;
    const long save_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
    emit_render_summary(pages_in_chunk, page_start, page_end, save_ms);
}

}  // namespace office_convert
