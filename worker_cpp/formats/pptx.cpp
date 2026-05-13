// pptx.cpp — Aspose.Slides-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-pptx binary.
//
// Page-range subsetting: Slides uses Presentation::Save with a slide-index
// array. RenderArgs page_start/page_end are 1-based inclusive; Slides'
// slide indices are also 1-based, so we pass them directly.

#include <DOM/ISlideCollection.h>
#include <DOM/Presentation.h>
#include <Export/PdfOptions.h>
#include <Export/SaveFormat.h>
#include <Util/License.h>
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

constexpr const char* kFormat = "pptx";

// Module-level document handle for pool mode.
System::SharedPtr<Aspose::Slides::Presentation> g_presentation;

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
        System::String(args.input.c_str()));
    render_pptx_from(pres, args.page_start, args.page_end, args.output);
}

int probe_pptx_slide_count(const std::string& input) {
    auto pres = System::MakeObject<Aspose::Slides::Presentation>(
        System::String(input.c_str()));
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
    g_presentation = System::MakeObject<Aspose::Slides::Presentation>(
        System::String(input_path.c_str()));
    return g_presentation->get_Slides()->get_Count();
}

void pool_render(int page_start, int page_end, const std::string& output_path) {
    if (!g_presentation) {
        throw RenderException("pool_render: no document loaded");
    }
    render_pptx_from(g_presentation, page_start, page_end, output_path);
}

}  // namespace office_convert
