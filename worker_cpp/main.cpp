// main.cpp — argv parsing, mode dispatch, exit code mapping for the
// per-product worker binary.
//
// Each binary (office-convert-worker-{docx,pptx,xlsx,pdf}) has the same
// CLI contract, but links exactly one Aspose product. The format passed in
// --format is validated against the binary's compiled-in product by the
// per-product dispatch_* functions; a mismatch yields an
// input_unprocessable diagnostic.
//
// CLI contract (component-methods.md §worker, unchanged from v1):
//   office-convert-worker-<fmt>
//       --mode render|probe
//       --input <path>
//       --format docx|pptx|xlsx|pdf      (must match this binary's <fmt>)
//       --license-path <path>
//       [--output <path>]                 (render mode only)
//       [--page-range <start>-<end>]      (render mode only)
//
// Exit codes (business-rules.md §2): 0/1/2/3/137.

#include <cstdlib>
#include <exception>
#include <iostream>
#include <map>
#include <string>

#include "error.h"
#include "license.h"
#include "pool.h"
#include "probe.h"
#include "render.h"

namespace {

void usage() {
    std::cerr << "usage: office-convert-worker --mode <render|probe> "
                 "--input <path> --format <docx|pptx|xlsx|pdf> "
                 "--license-path <path> "
                 "[--output <path>] [--page-range <s>-<e>]\n";
}

// Tiny argv parser. Each option is "--key value".
std::map<std::string, std::string> parse_args(int argc, char** argv) {
    std::map<std::string, std::string> out;
    for (int i = 1; i + 1 < argc; i += 2) {
        std::string key(argv[i]);
        std::string val(argv[i + 1]);
        if (key.rfind("--", 0) != 0) {
            // Malformed; let the caller decide what to do.
            continue;
        }
        out[key.substr(2)] = val;
    }
    return out;
}

bool parse_page_range(const std::string& range, int& start, int& end) {
    auto dash = range.find('-');
    if (dash == std::string::npos) {
        return false;
    }
    try {
        start = std::stoi(range.substr(0, dash));
        end = std::stoi(range.substr(dash + 1));
    } catch (...) {
        return false;
    }
    return start >= 1 && end >= start;
}

}  // namespace

int main(int argc, char** argv) {
    auto args = parse_args(argc, argv);

    auto require = [&](const std::string& key) -> std::string {
        auto it = args.find(key);
        if (it == args.end() || it->second.empty()) {
            usage();
            std::cerr << "missing --" << key << "\n";
            std::exit(office_convert::EXIT_RENDER_FAILURE);
        }
        return it->second;
    };

    try {
        const std::string mode = require("mode");
        const std::string format = require("format");
        const std::string license_path = require("license-path");

        if (mode == "pool") {
            // Pool mode: persistent worker that reads JSON commands from stdin.
            // License is applied inside pool_loop. No --input needed at startup.
            //
            // Optional --pool-size N enables fork-after-load: the leader loads
            // the document once, forks N-1 children that share the loaded
            // Document via copy-on-write, and routes seq-tagged render commands
            // across all N processes via socketpairs. Default N=1 = original
            // single-process behaviour.
            // Presence of --pool-size selects the seq-tagged fork-after-load
            // protocol (even with pool_size=1, where no fork happens but the
            // leader still talks the seq-tagged protocol the Python
            // ForkedPoolLeader expects). Absence keeps the legacy pool_loop
            // protocol used by WorkerPool's N-independent-subprocesses model.
            auto ps_it = args.find("pool-size");
            if (ps_it != args.end() && !ps_it->second.empty()) {
                int pool_size = 1;
                try { pool_size = std::stoi(ps_it->second); } catch (...) {}
                if (pool_size < 1) pool_size = 1;
                if (pool_size > 32) pool_size = 32;
                return office_convert::pool_loop_forked(format, license_path, pool_size);
            }
            return office_convert::pool_loop(format, license_path);
        }

        const std::string input = require("input");

        // The binary links exactly one Aspose product; apply_license is
        // resolved from that product's formats/<fmt>.cpp TU. No cross-product
        // activation overhead (the lazy-per-format trick of the old single
        // binary is now structural, not runtime-conditional).
        office_convert::apply_license(license_path);

        if (mode == "render") {
            office_convert::RenderArgs ra;
            ra.input = input;
            ra.output = require("output");
            ra.format = format;
            const std::string pr = require("page-range");
            if (!parse_page_range(pr, ra.page_start, ra.page_end)) {
                office_convert::emit_diagnostic("bad_args", "page-range: " + pr);
                return office_convert::EXIT_RENDER_FAILURE;
            }
            office_convert::dispatch_render(ra);
        } else if (mode == "probe") {
            office_convert::ProbeArgs pa;
            pa.input = input;
            pa.format = format;
            office_convert::dispatch_probe(pa);
        } else {
            office_convert::emit_diagnostic("bad_args", "mode: " + mode);
            return office_convert::EXIT_RENDER_FAILURE;
        }
    } catch (const std::exception& e) {
        return office_convert::translate_exception(e);
    } catch (...) {
        office_convert::emit_diagnostic("render_failed", "unknown exception");
        return office_convert::EXIT_RENDER_FAILURE;
    }

    return office_convert::EXIT_OK;
}
