// license.h — Aspose license activation for a single product.
//
// In the 4-binary split (post-2026-05-12 v2 ABI fix), each worker binary
// links exactly one Aspose product and owns one definition of this symbol
// in its corresponding formats/<fmt>.cpp. The format is implicit in the
// binary identity — no format parameter is needed.
//
// Throws LicenseException on failure.

#pragma once

#include <string>

namespace office_convert {

void apply_license(const std::string& license_path);

}  // namespace office_convert
