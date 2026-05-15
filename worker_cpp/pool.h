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

// Current process's pool index — 0 for the leader, 1..N-1 for forked children.
// Used by per-format load-progress callbacks to tag their output so the shared
// stderr pipe stays correlatable with the dashboard's per-worker view.
int current_pool_index();

// Run the fork-after-load variant: leader loads the document, forks pool_size-1
// child renderers that share the loaded Document via copy-on-write, and routes
// seq-tagged render commands across N processes via socketpairs. Same JSON
// protocol on the orchestrator-facing stdin/stdout, but every command and
// response now carries a "seq" integer so the leader can demux multiple
// in-flight renders across children.
int pool_loop_forked(const std::string& format, const std::string& license_path, int pool_size);

}  // namespace office_convert
