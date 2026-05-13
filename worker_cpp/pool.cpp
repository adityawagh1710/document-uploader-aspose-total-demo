// pool.cpp — Pool mode event loop implementation.
//
// Reads line-delimited JSON commands from stdin, dispatches to
// pool_load / pool_render (defined per-format in formats/<fmt>.cpp),
// writes JSON responses to stdout.
//
// Protocol:
//   → {"cmd":"load","input":"/path","license_path":"/path"}
//   ← {"status":"ok","page_count":N}
//
//   → {"cmd":"render","page_start":1,"page_end":25,"output":"/tmp/chunk.pdf"}
//   ← {"status":"ok","output":"/tmp/chunk.pdf"}
//
//   → {"cmd":"quit"}
//   ← (process exits with code 0)
//
// Error response:
//   ← {"status":"error","code":N,"detail":"..."}

#include "pool.h"

#include <iostream>
#include <sstream>
#include <string>

#include "error.h"
#include "license.h"

namespace office_convert {

namespace {

// Minimal JSON value extraction (avoids pulling in a JSON library).
// Finds "key": "value" or "key":"value" in a JSON string.
std::string json_string(const std::string& json, const std::string& key) {
    // Look for "key" followed by : and then "value"
    std::string needle = "\"" + key + "\"";
    auto pos = json.find(needle);
    if (pos == std::string::npos) return "";
    pos += needle.size();
    // Skip whitespace and colon
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t' || json[pos] == ':')) ++pos;
    // Expect opening quote
    if (pos >= json.size() || json[pos] != '"') return "";
    ++pos;  // skip the quote
    auto end = json.find('"', pos);
    if (end == std::string::npos) return "";
    return json.substr(pos, end - pos);
}

int json_int(const std::string& json, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    auto pos = json.find(needle);
    if (pos == std::string::npos) return -1;
    pos += needle.size();
    // Skip whitespace and colon
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t' || json[pos] == ':')) ++pos;
    try {
        return std::stoi(json.substr(pos));
    } catch (...) {
        return -1;
    }
}

void respond_ok(int page_count) {
    std::cout << "{\"status\":\"ok\",\"page_count\":" << page_count << "}" << std::endl;
}

void respond_ok_render(const std::string& output) {
    std::cout << "{\"status\":\"ok\",\"output\":\"" << output << "\"}" << std::endl;
}

void respond_error(int code, const std::string& detail) {
    // Escape quotes in detail
    std::string escaped;
    for (char c : detail) {
        if (c == '"') escaped += "\\\"";
        else if (c == '\\') escaped += "\\\\";
        else if (c == '\n') escaped += "\\n";
        else escaped += c;
    }
    std::cout << "{\"status\":\"error\",\"code\":" << code
              << ",\"detail\":\"" << escaped << "\"}" << std::endl;
}

}  // namespace

int pool_loop(const std::string& format, const std::string& license_path) {
    // Apply license once at startup
    try {
        apply_license(license_path);
    } catch (const WorkerError& e) {
        respond_error(e.exit_code(), e.what());
        return e.exit_code();
    } catch (const std::exception& e) {
        respond_error(EXIT_LICENSE_INVALID, e.what());
        return EXIT_LICENSE_INVALID;
    }

    std::string line;
    bool document_loaded = false;

    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;

        std::string cmd = json_string(line, "cmd");

        if (cmd == "quit") {
            return EXIT_OK;
        }

        if (cmd == "load") {
            std::string input = json_string(line, "input");
            if (input.empty()) {
                respond_error(EXIT_RENDER_FAILURE, "missing input path");
                continue;
            }
            try {
                int page_count = pool_load(input);
                document_loaded = true;
                respond_ok(page_count);
            } catch (const WorkerError& e) {
                respond_error(e.exit_code(), e.what());
            } catch (const std::bad_alloc&) {
                respond_error(EXIT_OOM, "load: bad_alloc");
                return EXIT_OOM;  // OOM is fatal for the process
            } catch (const std::exception& e) {
                respond_error(EXIT_RENDER_FAILURE, std::string("load: ") + e.what());
            }
            continue;
        }

        if (cmd == "render") {
            if (!document_loaded) {
                respond_error(EXIT_RENDER_FAILURE, "no document loaded");
                continue;
            }
            int page_start = json_int(line, "page_start");
            int page_end = json_int(line, "page_end");
            std::string output = json_string(line, "output");
            if (page_start < 1 || page_end < page_start || output.empty()) {
                respond_error(EXIT_RENDER_FAILURE, "bad render args");
                continue;
            }
            try {
                pool_render(page_start, page_end, output);
                respond_ok_render(output);
            } catch (const WorkerError& e) {
                respond_error(e.exit_code(), e.what());
            } catch (const std::bad_alloc&) {
                respond_error(EXIT_OOM, "render: bad_alloc");
                return EXIT_OOM;  // OOM is fatal
            } catch (const std::exception& e) {
                respond_error(EXIT_RENDER_FAILURE, std::string("render: ") + e.what());
            }
            continue;
        }

        respond_error(EXIT_RENDER_FAILURE, "unknown cmd: " + cmd);
    }

    // stdin closed (orchestrator died or pipe broken)
    return EXIT_OK;
}

}  // namespace office_convert
