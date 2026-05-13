// error.h — exit code constants and exception → exit translator.
//
// Implements the worker contract from business-rules.md §2 and FR-6.
// The exit code is the ONLY signal that crosses the subprocess boundary.

#pragma once

#include <exception>
#include <string>

namespace office_convert {

constexpr int EXIT_OK = 0;
constexpr int EXIT_RENDER_FAILURE = 1;
constexpr int EXIT_LICENSE_INVALID = 2;
constexpr int EXIT_INPUT_UNPROCESSABLE = 3;
constexpr int EXIT_OOM = 137;

// Custom exception classes for the worker side.
class WorkerError : public std::exception {
 public:
    WorkerError(int exit_code, std::string message)
        : exit_code_(exit_code), message_(std::move(message)) {}
    int exit_code() const noexcept { return exit_code_; }
    const char* what() const noexcept override { return message_.c_str(); }

 private:
    int exit_code_;
    std::string message_;
};

class OOMException : public WorkerError {
 public:
    explicit OOMException(const std::string& msg = "out of memory")
        : WorkerError(EXIT_OOM, msg) {}
};

class LicenseException : public WorkerError {
 public:
    explicit LicenseException(const std::string& msg = "license invalid")
        : WorkerError(EXIT_LICENSE_INVALID, msg) {}
};

class InputUnprocessableException : public WorkerError {
 public:
    explicit InputUnprocessableException(const std::string& msg = "input unprocessable")
        : WorkerError(EXIT_INPUT_UNPROCESSABLE, msg) {}
};

class RenderException : public WorkerError {
 public:
    explicit RenderException(const std::string& msg = "render failed")
        : WorkerError(EXIT_RENDER_FAILURE, msg) {}
};

// Translate any std::exception subclass to an exit code.
// Writes the diagnostic to stderr in a single line.
int translate_exception(const std::exception& e);

// Write a JSON-shaped diagnostic to stderr and return the exit code.
void emit_diagnostic(const std::string& failure_class, const std::string& detail);

}  // namespace office_convert
