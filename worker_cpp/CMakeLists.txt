# worker_cpp/CMakeLists.txt — build FOUR per-product office-convert workers.
#
# Background (the bug this solves):
#   The previous single-binary worker linked all 4 Aspose libraries. Words 26.3
#   and Slides/PDF 26.4 ship CodePorting.Translator.Cs2Cpp.Framework with the
#   SAME SONAME but different versions. Loading both into one process forces
#   the dynamic linker to pick one cs2cpp; Cells's plain-C++ Workbook
#   constructor then crashes during XLSX rendering.
#
# Fix (this file):
#   Build one binary per product. Each binary's DT_NEEDED set + RPATH cover
#   ONLY its product's .so files. No two CodePorting versions ever coexist in
#   a single process. Targets:
#     - office-convert-worker-docx  → Words   + Words' CodePorting 26.3 + HarfBuzz
#     - office-convert-worker-pptx  → Slides  + Slides' CodePorting 26.4
#     - office-convert-worker-xlsx  → Cells   (plain C++ — no CodePorting)
#     - office-convert-worker-pdf   → PDF.Cpp + PDF's bundled cs2cpp .so
#
# Each binary compiles:  main.cpp + error.cpp + formats/<fmt>.cpp
# where formats/<fmt>.cpp owns apply_license, dispatch_render, dispatch_probe
# for that single product. license.cpp / render.cpp / probe.cpp have been
# deleted — those entry points are per-binary now.
#
# Why manual IMPORTED targets instead of find_package:
#   Each product's CMake config calls find_dependency(CodePorting...). The
#   find_package cache is process-wide, so a single CMakeLists running 4
#   find_package calls would resolve all 4 against ONE cs2cpp — exactly the
#   bug we're fixing at runtime. Manual IMPORTED targets keep each binary's
#   link line under explicit, per-target control.
#
# Build prerequisites:
#   - Each vendor tree populated (`make verify-vendor`).
#   - gcc-12 / g++-12 (Debian 12 default) or clang with C++17 support.
#   - CMake 3.25+ for stable target_link_directories semantics.
#   - Runtime base must provide glibc ≥ 2.34 (Words 26.3 floor).

cmake_minimum_required(VERSION 3.25)
project(office_convert_workers LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Vendor roots — operator overrides via -DVENDOR_ROOT=... if needed.
set(VENDOR_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/aspose"
    CACHE PATH "Aspose vendor root (contains Words/, Cells/, Slides/, PDF/ subdirs)")
set(WORDS_VENDOR_ROOT  "${VENDOR_ROOT}/Words"  CACHE PATH "Aspose.Words vendor tree")
set(CELLS_VENDOR_ROOT  "${VENDOR_ROOT}/Cells"  CACHE PATH "Aspose.Cells vendor tree")
set(SLIDES_VENDOR_ROOT "${VENDOR_ROOT}/Slides" CACHE PATH "Aspose.Slides vendor tree")
set(PDF_VENDOR_ROOT    "${VENDOR_ROOT}/PDF"    CACHE PATH "Aspose.PDF vendor tree")

foreach(_root WORDS_VENDOR_ROOT CELLS_VENDOR_ROOT SLIDES_VENDOR_ROOT PDF_VENDOR_ROOT)
    if(NOT EXISTS "${${_root}}")
        message(FATAL_ERROR
            "${_root} not found at ${${_root}}. "
            "Run `make verify-vendor` from the project root.")
    endif()
endforeach()

# Where the runtime image places each product subtree. Drives INSTALL_RPATH
# for each binary so it resolves only its own product's siblings.
set(RUNTIME_ASPOSE_ROOT "/opt/aspose"
    CACHE PATH "Where the runtime image installs vendor/aspose/ (used in RPATH)")

# Strict warnings (CI should make these fatal).
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Tier-1 perf optimizations per aidlc-state.md "Hard Constraints":
# minimize dynamic-linker work per spawn (subprocess-per-chunk model).
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(
        $<$<CONFIG:Release>:-O2>
        $<$<CONFIG:Release>:-flto>
        $<$<CONFIG:Release>:-fvisibility=hidden>
        $<$<CONFIG:Release>:-fvisibility-inlines-hidden>
        $<$<CONFIG:Release>:-fdata-sections>
        $<$<CONFIG:Release>:-ffunction-sections>
    )
    add_link_options(
        $<$<CONFIG:Release>:-flto>
        $<$<CONFIG:Release>:-Wl,--gc-sections>
        $<$<CONFIG:Release>:-Wl,-s>
    )
endif()

find_package(Threads REQUIRED)

# -----------------------------------------------------------------------------
# Helper: locate exactly one .so matching a glob within a product subdir.
# Errors out if the count != 1 so missing/duplicate vendor files fail loudly.
# -----------------------------------------------------------------------------
function(_find_one_so out_var glob)
    file(GLOB _found "${glob}")
    list(LENGTH _found _count)
    if(NOT _count EQUAL 1)
        message(FATAL_ERROR "Expected exactly one .so matching '${glob}', found ${_count}: ${_found}")
    endif()
    list(GET _found 0 _path)
    set(${out_var} "${_path}" PARENT_SCOPE)
endfunction()

# Resolve concrete .so paths per product. Globs accommodate Aspose's
# arch/libc-suffixed filenames (e.g. _x86_64_libstdcpp_libc2.23.so).
_find_one_so(WORDS_SO     "${WORDS_VENDOR_ROOT}/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so")
_find_one_so(WORDS_HB_SO  "${WORDS_VENDOR_ROOT}/Aspose.Words.Shaping.HarfBuzz.Cpp/lib/libAspose.Words.Shaping.HarfBuzz.Cpp.so")
_find_one_so(WORDS_CS_SO  "${WORDS_VENDOR_ROOT}/CodePorting.Translator.Cs2Cpp.Framework/lib/libcodeporting.translator.cs2cpp.framework_x86_64*.so")

_find_one_so(CELLS_SO     "${CELLS_VENDOR_ROOT}/Aspose.Cells/lib/linux_x86_64/libAspose.Cells.so")

_find_one_so(SLIDES_SO    "${SLIDES_VENDOR_ROOT}/Aspose.Slides.Cpp/lib/libAspose.Slides_x86_64*.so")
_find_one_so(SLIDES_CS_SO "${SLIDES_VENDOR_ROOT}/CodePorting.Translator.Cs2Cpp.Framework/lib/libcodeporting.translator.cs2cpp.framework_x86_64*.so")

_find_one_so(PDF_SO       "${PDF_VENDOR_ROOT}/lib/libAspose.PDF.Cpp_x86_64*.so")
_find_one_so(PDF_CS_SO    "${PDF_VENDOR_ROOT}/lib/libcodeporting.translator.cs2cpp.framework_x86_64*.so")

# -----------------------------------------------------------------------------
# Per-product binary builder.
#
# Each binary:
#   - Compiles main.cpp + error.cpp + formats/<fmt>.cpp ONLY (no other format
#     source enters its translation set, so cross-product headers can't leak).
#   - Links exactly the .so files in ${LIBS}; DT_NEEDED is therefore strictly
#     scoped to one product's surface area.
#   - Sets INSTALL_RPATH to the runtime image's paths for that product only.
#
# Arguments:
#   NAME      — binary basename (docx|pptx|xlsx|pdf); produces office-convert-worker-<NAME>
#   FORMAT    — source file under formats/ (must match the binary's compiled-in product)
#   INCLUDES  — list of include directories
#   LIBS      — list of absolute .so paths to link
#   RPATH     — list of runtime image lib directories (joined with ':')
# -----------------------------------------------------------------------------
function(add_aspose_worker)
    cmake_parse_arguments(W "" "NAME;FORMAT" "INCLUDES;LIBS;RPATH" ${ARGN})

    set(_target "office-convert-worker-${W_NAME}")
    add_executable(${_target}
        main.cpp
        error.cpp
        pool.cpp
        formats/${W_FORMAT}.cpp
    )
    target_include_directories(${_target} PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}"
        ${W_INCLUDES}
    )
    target_link_libraries(${_target} PRIVATE ${W_LIBS} Threads::Threads)

    # Join RPATH list with ':' for both BUILD_RPATH (dev runs from build dir)
    # and INSTALL_RPATH (production layout under /opt/aspose/).
    list(JOIN W_RPATH ":" _rpath)
    set_target_properties(${_target} PROPERTIES
        BUILD_RPATH   "\$ORIGIN/../lib:${_rpath}"
        INSTALL_RPATH "\$ORIGIN/../lib:${_rpath}"
    )

    install(TARGETS ${_target} RUNTIME DESTINATION bin)
endfunction()

# -----------------------------------------------------------------------------
# DOCX worker — Aspose.Words 26.3.
# Uses Words' OWN CodePorting 26.3 (in the old single-binary design this was
# masked by Slides' 26.4 to suppress version warnings; per-binary isolation
# means each product gets its own matching cs2cpp again).
# -----------------------------------------------------------------------------
add_aspose_worker(
    NAME docx
    FORMAT docx
    INCLUDES
        "${WORDS_VENDOR_ROOT}/Aspose.Words.Cpp/include"
        "${WORDS_VENDOR_ROOT}/CodePorting.Translator.Cs2Cpp.Framework/include"
    LIBS
        "${WORDS_SO}"
        "${WORDS_HB_SO}"
        "${WORDS_CS_SO}"
    RPATH
        "${RUNTIME_ASPOSE_ROOT}/Words/Aspose.Words.Cpp/lib"
        "${RUNTIME_ASPOSE_ROOT}/Words/Aspose.Words.Shaping.HarfBuzz.Cpp/lib"
        "${RUNTIME_ASPOSE_ROOT}/Words/CodePorting.Translator.Cs2Cpp.Framework/lib"
)

# -----------------------------------------------------------------------------
# PPTX worker — Aspose.Slides 26.4.
# -----------------------------------------------------------------------------
add_aspose_worker(
    NAME pptx
    FORMAT pptx
    INCLUDES
        "${SLIDES_VENDOR_ROOT}/Aspose.Slides.Cpp/include/aspose.slides.cpp"
        "${SLIDES_VENDOR_ROOT}/CodePorting.Translator.Cs2Cpp.Framework/include"
    LIBS
        "${SLIDES_SO}"
        "${SLIDES_CS_SO}"
    RPATH
        "${RUNTIME_ASPOSE_ROOT}/Slides/Aspose.Slides.Cpp/lib"
        "${RUNTIME_ASPOSE_ROOT}/Slides/CodePorting.Translator.Cs2Cpp.Framework/lib"
)

# -----------------------------------------------------------------------------
# XLSX worker — Aspose.Cells 26.4 (plain C++, no CodePorting).
# This binary is the load-bearing fix: with no cs2cpp .so in its DT_NEEDED,
# Cells's Workbook constructor no longer collides with Slides/Words state.
# -----------------------------------------------------------------------------
add_aspose_worker(
    NAME xlsx
    FORMAT xlsx
    INCLUDES
        "${CELLS_VENDOR_ROOT}/Aspose.Cells/include"
    LIBS
        "${CELLS_SO}"
    RPATH
        "${RUNTIME_ASPOSE_ROOT}/Cells/Aspose.Cells/lib/linux_x86_64"
)

# -----------------------------------------------------------------------------
# PDF worker — Aspose.PDF 26.4 + its bundled CodePorting copy.
# PDF ships a flat layout under PDF/lib/ containing both the product .so and
# its private cs2cpp .so; the runtime image preserves that layout.
# -----------------------------------------------------------------------------
add_aspose_worker(
    NAME pdf
    FORMAT pdf
    INCLUDES
        "${PDF_VENDOR_ROOT}/include"
        "${PDF_VENDOR_ROOT}/include/asposecpplib"
    LIBS
        "${PDF_SO}"
        "${PDF_CS_SO}"
    RPATH
        "${RUNTIME_ASPOSE_ROOT}/PDF/lib"
)
