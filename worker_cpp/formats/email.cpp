// email.cpp — Aspose.Email-only TU. Owns apply_license, dispatch_render,
// dispatch_probe for the office-convert-worker-email binary.
//
// Unlike the other 4 workers, this one does NOT produce PDF. It produces
// MHTML — a self-contained HTML+attachments archive. The Python shim
// (office_convert.aspose_email_convert) then pipes that MHT through the
// existing worker-docx binary for the MHTML → PDF step. The two-stage
// pipeline keeps Aspose.Email's cs2cpp 25.12 framework isolated from
// Aspose.Words's cs2cpp 26.3 framework: they never coexist in one process.
//
// Pool mode is not implemented — emails are short and single-shot, with
// no pagination concept worth a persistent loaded handle.

#include <Licensing/License.h>
#include <MailMessage.h>
#include <MhtSaveOptions.h>
#include <SaveOptions.h>
#include <system/object.h>
#include <system/shared_ptr.h>
#include <system/string.h>

#include <cstdio>
#include <exception>
#include <string>

#include "../error.h"
#include "../license.h"
#include "../pool.h"
#include "../probe.h"
#include "../probe_util.h"
#include "../render.h"

namespace office_convert {

namespace {

constexpr const char* kFormat = "email";

void verify_license_file(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (f == nullptr) {
        throw LicenseException("license file not readable: " + path);
    }
    std::fclose(f);
}

void render_email_to_mht(const RenderArgs& args) {
    auto msg = Aspose::Email::MailMessage::Load(System::String(args.input.c_str()));
    // get_DefaultMhtml() returns SharedPtr<MhtSaveOptions>; Save() takes
    // SharedPtr<SaveOptions>. Upcast through an explicit base-typed variable
    // so the SharedPtr template's converting constructor kicks in.
    System::SharedPtr<Aspose::Email::SaveOptions> opts =
        Aspose::Email::SaveOptions::get_DefaultMhtml();
    msg->Save(System::String(args.output.c_str()), opts);
}

}  // namespace

void apply_license(const std::string& license_path) {
    verify_license_file(license_path);
    try {
        auto license = System::MakeObject<Aspose::Email::License>();
        license->SetLicense(System::String(license_path.c_str()));
    } catch (const std::exception& e) {
        throw LicenseException(std::string("Aspose::Email SetLicense: ") + e.what());
    }
}

void dispatch_render(const RenderArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-email invoked with format=" + args.format);
    }
    try {
        render_email_to_mht(args);
    } catch (const WorkerError&) {
        throw;
    } catch (const std::bad_alloc&) {
        throw OOMException("EMAIL render bad_alloc");
    } catch (const std::exception& e) {
        throw RenderException(std::string("EMAIL: ") + e.what());
    }
}

void dispatch_probe(const ProbeArgs& args) {
    if (args.format != kFormat) {
        throw InputUnprocessableException("worker-email invoked with format=" + args.format);
    }
    // Emails don't paginate. Report a single logical page so the orchestrator's
    // chunk planner has a well-formed ProbeResult, even though this worker is
    // dispatched outside the chunking path.
    emit_probe_json(1, kFormat, file_size_bytes(args.input));
}

// Pool mode is not supported for emails. Provide stubs so the binary links;
// pool_loop will never call them because the Python orchestrator routes EML
// outside the persistent-pool path.

int pool_load(const std::string& /*input_path*/) {
    throw RenderException("pool mode not supported for email format");
}

void pool_render(int /*page_start*/, int /*page_end*/, const std::string& /*output_path*/) {
    throw RenderException("pool mode not supported for email format");
}

}  // namespace office_convert
