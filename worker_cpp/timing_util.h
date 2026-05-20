// timing_util.h — shared `emit_timing_ms` helper used by the pool-mode
// instrumentation in worker_cpp/formats/{docx,pptx,pdf}.cpp.
//
// xlsx.cpp still carries its own copy of this helper (anonymous namespace,
// identical body) because it landed first and the working code is left
// untouched. A future cleanup pass could de-dupe it; for now both work
// because xlsx's version is TU-local and won't collide with the shared one.
//
// Output format (single line per call, stderr):
//   {"type":"timing","stage":"<stage>","duration_ms":<int>}
//
// The Python stderr reader in office_convert/worker_pool.py picks lines
// with "type":"timing" and forwards them to office_convert.timings.timing_store
// keyed by request_id. test_ui.py reads /jobs/{id}/timings and renders the
// Time-per-stage + Chunk Gantt charts from those events.

#pragma once

#include <chrono>
#include <iostream>

namespace office_convert {

inline void emit_timing_ms(const char* stage,
                           std::chrono::steady_clock::duration d) {
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(d).count();
    std::cerr << "{\"type\":\"timing\",\"stage\":\"" << stage
              << "\",\"duration_ms\":" << ms << "}\n"
              << std::flush;
}

// Emit the per-chunk pool_render summary: pages + boundaries + total save
// ms + per-page ms. Mirrors the shape xlsx.cpp emits inline so the
// Streamlit chart's parsing path works identically for any format.
inline void emit_render_summary(int pages_in_chunk,
                                int page_start,
                                int page_end,
                                long save_ms) {
    const long per_page_ms = pages_in_chunk > 0 ? save_ms / pages_in_chunk : 0L;
    std::cerr << "{\"type\":\"timing\",\"stage\":\"pool_render.summary\""
              << ",\"pages\":" << pages_in_chunk
              << ",\"page_start\":" << page_start
              << ",\"page_end\":" << page_end
              << ",\"save_ms\":" << save_ms
              << ",\"per_page_ms\":" << per_page_ms
              << "}\n"
              << std::flush;
}

}  // namespace office_convert
