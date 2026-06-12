// docx.cpp — Aspose.Words-only TU. Owns apply_license, dispatch_render,
// dispatch_probe, pool_load, pool_render for the office-convert-worker-docx binary.
//
// Page-range subsetting via PdfSaveOptions::set_PageSet with explicit
// zero-based page indices.
//
// This binary also renders format=html (single-shot, full document): Words
// loads HTML natively, so HTML→PDF reuses the docx worker instead of shipping
// a sixth Aspose product. The html path ignores --page-range and enforces the
// BR-4 external-resource deny policy via IResourceLoadingCallback.

#include <Aspose.Words.Cpp/Document.h>
#include <Aspose.Words.Cpp/Licensing/License.h>
#include <Aspose.Words.Cpp/LoadFormat.h>
#include <Aspose.Words.Cpp/Loading/DocumentLoadingArgs.h>
#include <Aspose.Words.Cpp/Loading/IDocumentLoadingCallback.h>
#include <Aspose.Words.Cpp/Loading/IResourceLoadingCallback.h>
#include <Aspose.Words.Cpp/Loading/LoadOptions.h>
#include <Aspose.Words.Cpp/Loading/ResourceLoadingAction.h>
#include <Aspose.Words.Cpp/Loading/ResourceLoadingArgs.h>
#include <Aspose.Words.Cpp/PageSetup.h>
#include <Aspose.Words.Cpp/Saving/PageSet.h>
#include <Aspose.Words.Cpp/Saving/PdfSaveOptions.h>
#include <Aspose.Words.Cpp/Section.h>
#include <Aspose.Words.Cpp/SectionCollection.h>
#include <system/array.h>
#include <system/object.h>
#include <system/string.h>

#include <cctype>
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

// --- HTML render path (format=html) ---

// BR-4 deny policy (functional-design business-rules.md): external resources
// referenced by uploaded HTML are skipped unless they are http(s) URLs to
// public hosts. Loopback, RFC1918, link-local/metadata, IPv6-private, and
// single-label (in-cluster) hostnames are denied. Mirrors the orchestrator's
// internal/netpolicy and Gotenberg's --chromium-deny-list — keep in sync.
bool ipv4_octets(const std::string& host, int out[4]) {
    int vals[4] = {0, 0, 0, 0};
    int idx = 0;
    size_t start = 0;
    for (size_t i = 0; i <= host.size(); ++i) {
        if (i == host.size() || host[i] == '.') {
            if (i == start || idx > 3) return false;
            for (size_t j = start; j < i; ++j) {
                if (!std::isdigit(static_cast<unsigned char>(host[j]))) return false;
            }
            try {
                vals[idx] = std::stoi(host.substr(start, i - start));
            } catch (...) {
                return false;
            }
            if (vals[idx] < 0 || vals[idx] > 255) return false;
            ++idx;
            start = i + 1;
        }
    }
    if (idx != 4) return false;
    for (int i = 0; i < 4; ++i) out[i] = vals[i];
    return true;
}

bool deny_resource_url(const std::string& raw, std::string& reason) {
    std::string lower;
    lower.reserve(raw.size());
    for (char c : raw) lower.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));

    // data: URIs embed their payload — no network fetch, allow.
    if (lower.rfind("data:", 0) == 0) return false;

    const auto scheme_end = lower.find("://");
    if (scheme_end == std::string::npos) {
        reason = "non-absolute or non-http(s) resource";
        return true;
    }
    const std::string scheme = lower.substr(0, scheme_end);
    if (scheme != "http" && scheme != "https") {
        reason = "scheme " + scheme;
        return true;
    }

    // Host = authority minus userinfo/port/path.
    std::string host = lower.substr(scheme_end + 3);
    if (auto at = host.find('@'); at != std::string::npos) host = host.substr(at + 1);
    if (auto slash = host.find_first_of("/?#"); slash != std::string::npos) host = host.substr(0, slash);
    if (!host.empty() && host.front() == '[') {  // IPv6 literal
        const auto close = host.find(']');
        const std::string v6 = close == std::string::npos ? host.substr(1) : host.substr(1, close - 1);
        if (v6 == "::1" || v6.rfind("fd", 0) == 0 || v6.rfind("fc", 0) == 0 ||
            v6.rfind("fe8", 0) == 0 || v6.rfind("fe9", 0) == 0 ||
            v6.rfind("fea", 0) == 0 || v6.rfind("feb", 0) == 0) {
            reason = "private IPv6 " + v6;
            return true;
        }
        return false;
    }
    if (auto colon = host.find(':'); colon != std::string::npos) host = host.substr(0, colon);

    if (host.empty()) {
        reason = "empty host";
        return true;
    }
    if (host == "localhost" || (host.size() > 10 && host.compare(host.size() - 10, 10, ".localhost") == 0)) {
        reason = "loopback host";
        return true;
    }
    int o[4];
    if (ipv4_octets(host, o)) {
        const bool priv = o[0] == 127 || (o[0] == 0 && o[1] == 0 && o[2] == 0 && o[3] == 0) ||
                          o[0] == 10 || (o[0] == 172 && o[1] >= 16 && o[1] <= 31) ||
                          (o[0] == 192 && o[1] == 168) || (o[0] == 169 && o[1] == 254);
        if (priv) {
            reason = "private IPv4 " + host;
            return true;
        }
        return false;
    }
    if (host.find('.') == std::string::npos) {
        reason = "single-label host " + host;
        return true;
    }
    return false;
}

class DenyPolicyResourceCallback
    : public Aspose::Words::Loading::IResourceLoadingCallback {
public:
    Aspose::Words::Loading::ResourceLoadingAction ResourceLoading(
        System::SharedPtr<Aspose::Words::Loading::ResourceLoadingArgs> args) override {
        const std::string uri = args->get_OriginalUri().ToUtf8String();
        std::string reason;
        if (deny_resource_url(uri, reason)) {
            // Hostname-level audit line (NFR-4); never the full URI or content.
            std::cerr << "{\"type\":\"resource_denied\",\"reason\":\"" << reason << "\"}\n"
                      << std::flush;
            return Aspose::Words::Loading::ResourceLoadingAction::Skip;
        }
        return Aspose::Words::Loading::ResourceLoadingAction::Default;
    }
};

// BR-7 fair-comparison geometry: US Letter, 0.5in margins, on every section
// (both engines render to identical page geometry). 1in == 72pt.
void force_letter_geometry(System::SharedPtr<Aspose::Words::Document> doc) {
    auto sections = doc->get_Sections();
    for (int i = 0; i < sections->get_Count(); ++i) {
        auto ps = sections->idx_get(i)->get_PageSetup();
        ps->set_PageWidth(612.0);   // 8.5in
        ps->set_PageHeight(792.0);  // 11in
        ps->set_TopMargin(36.0);    // 0.5in
        ps->set_BottomMargin(36.0);
        ps->set_LeftMargin(36.0);
        ps->set_RightMargin(36.0);
    }
}

// Full-document HTML render. --page-range is deliberately ignored: HTML has no
// pre-known page count and this path is single-shot (no chunk planner).
void render_html(const RenderArgs& args) {
    auto load_opts = System::MakeObject<Aspose::Words::Loading::LoadOptions>();
    load_opts->set_LoadFormat(Aspose::Words::LoadFormat::Html);
    load_opts->set_ResourceLoadingCallback(
        System::MakeObject<DenyPolicyResourceCallback>());
    auto doc = System::MakeObject<Aspose::Words::Document>(
        System::String(args.input.c_str()), load_opts);
    force_letter_geometry(doc);

    auto opts = System::MakeObject<Aspose::Words::Saving::PdfSaveOptions>();
    opts->set_TempFolder(System::String("/tmp"));
    opts->set_MemoryOptimization(true);
    doc->Save(System::String(args.output.c_str()), opts);
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
    if (args.format != kFormat && args.format != "html") {
        throw InputUnprocessableException("worker-docx invoked with format=" + args.format);
    }
    try {
        if (args.format == "html") {
            render_html(args);
        } else {
            render_docx(args);
        }
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
