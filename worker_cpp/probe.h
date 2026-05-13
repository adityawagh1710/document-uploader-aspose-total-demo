// probe.h — probe entry point for the per-product worker binary.
//
// In the 4-binary split, each binary owns one definition of dispatch_probe
// in its formats/<fmt>.cpp. Emits JSON ProbeResult on stdout.
//
// Schema:
//   {"page_count": N, "format": "...", "natural_seams": [[s,e], ...], "size_bytes": N}

#pragma once

#include <string>

namespace office_convert {

struct ProbeArgs {
    std::string input;
    std::string format;
};

void dispatch_probe(const ProbeArgs& args);

}  // namespace office_convert
