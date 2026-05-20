// pptx.cpp — Aspose.Slides-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-pptx binary.
//
// Page-range subsetting: Slides uses Presentation::Save with a slide-index
// array. RenderArgs page_start/page_end are 1-based inclusive; Slides'
// slide indices are also 1-based, so we pass them directly.

#include <DOM/ISlideCollection.h>
#include <DOM/LoadOptions.h>
#include <DOM/Presentation.h>
#include <Export/PdfOptions.h>
#include <Export/SaveFormat.h>
#include <LoadFormat.h>
#include <Util/License.h>
#include <system/array.h>
#include <system/object.h>
#include <system/string.h>

#include <chrono>
#include <cstdio>
#include <cstring>
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

constexpr const char* kFormat = "pptx";

// Module-level document handle for pool mode.
System::SharedPtr<Aspose::Slides::Presentation> g_presentation;

// Aspose.Slides' default loader fails on ODP for the same reason
// Aspose.Words fails on ODT (see docx.cpp): zip-magic file without OOXML
// structure → FileCorruptedException. Force the LoadFormat::Odp hint when
// the input filename advertises ODF Presentation (.odp) or its template (.otp).
bool input_ends_with_odp(const std::string& path) {
    auto ends = [&](const char* suffix) {
        size_t n = std::strlen(suffix);
        return path.size() >= n
            && path.compare(path.size() - n, n, suffix) == 0;
    };
    return ends(".odp") || ends(".otp");
}

System::SharedPtr<Aspose::Slides::LoadOptions> make_load_opts(const std::string& input) {
    auto opts = System::MakeObject<Aspose::Slides::LoadOptions>();
    if (input_ends_with_odp(input)) {
        opts->set_LoadFormat(Aspose::Slides::LoadFormat::Odp);
    }
    return opts;
}

void verify_license_file(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (f == nullptr) {
        throw LicenseException("license file not readable: " + path);
    }
    std::fclose(f);
}

void render_pptx_from(System::SharedPtr<Aspose::Slides::Presentation> pres,
                      int page_start, int page_end,
                      const std::string& output) {
    const int count = page_end - page_start + 1;
    auto slides = System::MakeArray<int32_t>(count);
    for (int i = 0; i < count; ++i) {
        slides[i] = page_start + i;  // 1-based slide indices
    }
    auto opts = System::MakeObject<Aspose::Slides::Export::PdfOptions>();
    pres->Save(System::String(output.c_str()),
               slides,
               Aspose::Slides::Export::SaveFormat::Pdf,
               opts);
}

void render_pptx(const RenderArgs& args) {
    auto pres = System::MakeObject<Aspose::Slides::Presentation>(
        System::String(args.input.c_str()), make_load_opts(args.input));
    render_pptx_from(pres, args.page_start, args.page_end, args.output);
}

int probe_pptx_slide_count(const std::string& input) {
    auto pres = System::MakeObject<Aspose::Slides::Presentation>(
        System::String(input.c_str()), make_load_opts(input));
    return pres->get_Slides()->get_Count();
}

}  // namespace

void apply_license(const std::string& license_path) {
    verify_license_file(license_path);
    try {
        auto license = System::MakeObject<Aspose::Slides::License>();
        license->SetLicense(System::String(license_path.c_str()));
    } catch (const std::exception& e) {
        throw LicenseException(std::string("Aspose::Slides SetLicense: ") + e.what());
    }
}

void dispatch_render(const RenderArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-pptx invoked with format=" + args.format);
    }
    try {
        render_pptx(args);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("PPTX render bad_alloc");
    } catch (const std::exception& e) {
        throw RenderException(std::string("PPTX: ") + e.what());
    }
}

void dispatch_probe(const ProbeArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-pptx invoked with format=" + args.format);
    }
    int slide_count = 0;
    try {
        slide_count = probe_pptx_slide_count(args.input);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("probe bad_alloc");
    } catch (const std::exception& e) {
        throw InputUnprocessableException(std::string("probe: ") + e.what());
    }
    emit_probe_json(slide_count, kFormat, file_size_bytes(args.input));
}

// --- Pool mode ---

int pool_load(const std::string& input_path) {
    auto t0 = std::chrono::steady_clock::now();
    g_presentation = System::MakeObject<Aspose::Slides::Presentation>(
        System::String(input_path.c_str()), make_load_opts(input_path));
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.presentation_load", t1 - t0);

    auto t2 = std::chrono::steady_clock::now();
    int slide_count = g_presentation->get_Slides()->get_Count();
    auto t3 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_load.slide_count", t3 - t2);

    return slide_count;
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (!g_presentation) {
        throw RenderException("pool_render: no document loaded");
    }

    auto t0 = std::chrono::steady_clock::now();
    render_pptx_from(g_presentation, page_start, page_end, output_path);
    auto t1 = std::chrono::steady_clock::now();
    emit_timing_ms("pool_render.save", t1 - t0);

    const int pages_in_chunk = page_end - page_start + 1;
    const long save_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
    emit_render_summary(pages_in_chunk, page_start, page_end, save_ms);
}

}  // namespace office_convert
