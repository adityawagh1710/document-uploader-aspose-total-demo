// pool.h — Pool mode declarations for persistent worker processes.
//
// Pool mode keeps the document loaded in memory and renders multiple
// page ranges on demand via stdin/stdout JSON protocol. This eliminates
// the per-chunk document-load overhead (the dominant cost for large files).
//
// Each per-product formats/<fmt>.cpp implements these functions.
// The document handle is stored as a module-level global (one document
// per process lifetime in pool mode).

#pragma once

#include <string>

namespace office_convert {

// Load a document into memory. Returns the page/slide count.
// Throws on failure (same exceptions as dispatch_render).
int pool_load(const std::string& input_path);

// Render a page range from the already-loaded document.
// Throws on failure (same exceptions as dispatch_render).
void pool_render(int page_start, int page_end, const std::string& output_path);

// Run the pool event loop: read JSON commands from stdin, dispatch, respond on stdout.
// Returns the exit code for the process.
int pool_loop(const std::string& format, const std::string& license_path);

}  // namespace office_convert
