// error.cpp — exception translation + stderr diagnostic helper.

#include "error.h"

#include <iostream>
#include <new>      // for std::bad_alloc
#include <string>

namespace office_convert {

int translate_exception(const std::exception& e) {
    // bad_alloc → OOM. Note: kernel SIGKILL on RLIMIT_AS overflow produces
    // exit status 137 directly (no exception is caught), which Python's
    // _map_exit_code handles identically to our explicit OOM path.
    if (dynamic_cast<const std::bad_alloc*>(&e) != nullptr) {
        emit_diagnostic("oom", "std::bad_alloc");
        return EXIT_OOM;
    }
    if (auto* w = dynamic_cast<const OOMException*>(&e); w != nullptr) {
        emit_diagnostic("oom", w->what());
        return w->exit_code();
    }
    if (auto* w = dynamic_cast<const LicenseException*>(&e); w != nullptr) {
        emit_diagnostic("license_invalid", w->what());
        return w->exit_code();
    }
    if (auto* w = dynamic_cast<const InputUnprocessableException*>(&e); w != nullptr) {
        emit_diagnostic("input_unprocessable", w->what());
        return w->exit_code();
    }
    // Default: classify as render failure.
    emit_diagnostic("render_failed", e.what());
    return EXIT_RENDER_FAILURE;
}

void emit_diagnostic(const std::string& failure_class, const std::string& detail) {
    // Single-line JSON for the orchestrator's stderr_tail capture.
    std::cerr << R"({"failure_class":")" << failure_class
              << R"(","detail":")" << detail << R"("})"
              << std::endl;
}

}  // namespace office_convert
