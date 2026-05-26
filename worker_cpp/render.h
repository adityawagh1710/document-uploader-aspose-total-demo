// render.h — render entry point for the per-product worker binary.
//
// In the 4-binary split, each binary owns one definition of dispatch_render
// in its formats/<fmt>.cpp. args.format is validated against the binary's
// compiled-in product and rejected on mismatch.

#pragma once

#include <string>

namespace office_convert {

struct RenderArgs {
    std::string input;
    std::string output;
    std::string format;  // "docx" | "pptx" | "xlsx" | "pdf" | "email"
    int page_start = 1;
    int page_end = 1;
};

void dispatch_render(const RenderArgs& args);

}  // namespace office_convert
