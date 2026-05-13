// probe_util.h — shared, Aspose-free helpers for emitting the probe JSON.
// Header-only so per-product binaries don't need to compile a shared .cpp.

#pragma once

#include <sys/stat.h>

#include <iostream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace office_convert {

inline long long file_size_bytes(const std::string& path) {
    struct stat st;
    if (::stat(path.c_str(), &st) != 0) {
        return 0;
    }
    return static_cast<long long>(st.st_size);
}

inline void emit_probe_json(int page_count,
                            const std::string& format,
                            long long size_bytes,
                            const std::vector<std::pair<int, int>>& seams = {}) {
    std::ostringstream s;
    s << "{\"page_count\":" << page_count
      << ",\"format\":\"" << format << "\""
      << ",\"size_bytes\":" << size_bytes
      << ",\"natural_seams\":[";
    bool first = true;
    for (const auto& seam : seams) {
        if (!first) s << ",";
        s << "[" << seam.first << "," << seam.second << "]";
        first = false;
    }
    s << "]}";
    std::cout << s.str();
}

}  // namespace office_convert
