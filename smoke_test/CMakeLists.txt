# Smoke test build — links ONLY against Aspose.Words from vendor/aspose/Words/.
# Adapted from the quickstart/CMakeLists.txt shipped inside Aspose.Words.Cpp_26.3.zip.
# Pre-integration validation; delete this directory after the production
# Dockerfile + worker_cpp/CMakeLists.txt refactor lands.

cmake_minimum_required(VERSION 3.21.0 FATAL_ERROR)
project(words_smoke CXX)

# Vendor root: contains Aspose.Words.Cpp/, Aspose.Words.Shaping.HarfBuzz.Cpp/,
# CodePorting.Translator.Cs2Cpp.Framework/ as sibling directories — the layout
# Aspose ships in its release zips.
set(WORDS_VENDOR_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/aspose/Words"
    CACHE PATH "Aspose.Words vendor root (defaults to project's vendor/aspose/Words/)")

if(NOT EXISTS "${WORDS_VENDOR_ROOT}/Aspose.Words.Cpp")
    message(FATAL_ERROR
        "Aspose.Words vendor root not found at ${WORDS_VENDOR_ROOT}. "
        "Run `make smoke-words` from the project root, which builds inside Docker "
        "with the vendor path pre-mounted, OR override -DWORDS_VENDOR_ROOT=...")
endif()

find_package(CodePorting.Translator.Cs2Cpp.Framework REQUIRED CONFIG
    PATHS "${WORDS_VENDOR_ROOT}" NO_DEFAULT_PATH)
find_package(Aspose.Words.Cpp REQUIRED CONFIG
    PATHS "${WORDS_VENDOR_ROOT}" NO_DEFAULT_PATH)

find_package(Threads REQUIRED)

add_executable(words_smoke words_smoke.cpp)

target_link_libraries(words_smoke PRIVATE
    Aspose::Words
    Threads::Threads
)

# Aspose requires at least C++14.
set_target_properties(words_smoke PROPERTIES
    CXX_STANDARD 14
    CXX_STANDARD_REQUIRED TRUE
)

# Prefer the gold linker on GCC for faster linking against the 174 MB Words .so.
if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    execute_process(
        COMMAND ${CMAKE_CXX_COMPILER} -fuse-ld=gold -Wl,--version
        ERROR_QUIET OUTPUT_VARIABLE LD_VERSION)
    if("${LD_VERSION}" MATCHES "GNU gold")
        target_link_options(words_smoke PRIVATE -fuse-ld=gold)
    endif()
endif()
