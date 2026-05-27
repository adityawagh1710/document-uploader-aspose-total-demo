# Multi-stage Dockerfile for office-convert (5-binary per-product split,
# post-2026-05-12 v2 ABI fix; Email worker added 2026-05-26).
#
# Stage 1 (builder): compile FIVE `office-convert-worker-<fmt>` binaries,
# each linking exactly one Aspose product from `vendor/aspose/`. The split
# resolves the cs2cpp framework SONAME collision between Words 26.3 and
# Slides/PDF 26.4 that previously wedged XLSX rendering; Email 25.12 brings
# its own cs2cpp 25.12 and stays isolated via the same mechanism.
# Stage 2 (runtime): slim Python 3.12 image with FastAPI + qpdf + all five
# worker binaries + each product's full .so tree (per-binary RPATHs keep
# them isolated at load time).
#
# Operator prerequisite: `vendor/aspose/` populated. See README.md SDK
# acquisition section; `make verify-vendor` validates the layout.
#
# Build:
#     docker build -t office-convert .
#
# Run (localhost-only, license bind-mounted):
#     docker run --rm \
#         -p 127.0.0.1:8080:8080 \
#         -v $(pwd)/Aspose.TotalforC++.lic:/aspose/license.lic:ro \
#         --cap-drop=ALL --read-only --tmpfs /tmp --tmpfs /var/run \
#         office-convert

# =============================================================================
# Stage 1: builder — gcc-12 + cmake + the 4 Aspose vendor trees → worker binary
# =============================================================================
FROM debian:bookworm AS builder

# `apt-get upgrade` picks up the latest Debian Bookworm point-release fixes
# for base-image-inherited packages (gnutls28, glibc, systemd, expat,
# libxml2, etc.) — without it, our images carry whatever CVE-vulnerable
# versions were in the base image when it was tagged. ECR scan went from
# ~26 CRITICAL/HIGH/MEDIUM findings → minimal after this change.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        gcc-12 g++-12 \
        cmake \
        make \
        build-essential \
        libfontconfig1 \
        libfontconfig-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV CC=gcc-12 CXX=g++-12

# Vendor trees (each product gets its own subdir under /opt/aspose/ to
# keep CodePorting framework siblings isolated per product).
COPY vendor/aspose/Words  /opt/aspose/Words
COPY vendor/aspose/Cells  /opt/aspose/Cells
COPY vendor/aspose/Slides /opt/aspose/Slides
COPY vendor/aspose/PDF    /opt/aspose/PDF
COPY vendor/aspose/Email  /opt/aspose/Email

# Worker sources.
WORKDIR /build
COPY worker_cpp/ ./worker_cpp/

# Build all 4 worker binaries with vendor pointing at the builder image's
# /opt/aspose/ layout. Each binary's INSTALL_RPATH already references
# /opt/aspose/<Product>/... so the runtime layout below mirrors this one.
# `cmake --build` with multiple --target flags drives parallel per-target
# compilation; -j keeps the underlying make/ninja saturated.
RUN cmake -S worker_cpp -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DVENDOR_ROOT=/opt/aspose \
        -DRUNTIME_ASPOSE_ROOT=/opt/aspose \
    && cmake --build build -j"$(nproc)" \
        --target office-convert-worker-docx \
                 office-convert-worker-pptx \
                 office-convert-worker-xlsx \
                 office-convert-worker-pdf \
                 office-convert-worker-email

# =============================================================================
# Stage 2: runtime — slim Python + qpdf + the worker binary + Aspose .so files
# =============================================================================
FROM python:3.12-slim-bookworm AS runtime

# Runtime deps. libfontconfig1 is required by Aspose.Words rendering;
# libfreetype6 + libpng16 + libxml2 satisfy other Aspose transitive needs.
# `apt-get upgrade` patches the inherited base image (see builder stage
# comment above for the rationale).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        qpdf \
        util-linux \
        libstdc++6 \
        libgcc-s1 \
        libfontconfig1 \
        libfreetype6 \
        libpng16-16 \
        libexpat1 \
        libuuid1 \
        libxml2 \
        ca-certificates \
        fontconfig \
        fonts-dejavu-core \
        libreoffice-core-nogui \
        libreoffice-draw-nogui \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# Non-root user.
RUN groupadd --system --gid 1000 appgroup \
    && useradd --system --uid 1000 --gid appgroup --home /app --shell /usr/sbin/nologin appuser

# Install Python dependencies via uv.
COPY pyproject.toml /app/pyproject.toml
WORKDIR /app
RUN pip install --no-cache-dir uv==0.5.* \
    && uv pip install --system --no-cache \
        "fastapi==0.115.*" \
        "uvicorn[standard]==0.32.*" \
        "pydantic==2.9.*" \
        "pydantic-settings==2.6.*" \
        "aiofiles==24.1.*" \
        "python-multipart==0.0.12" \
        "boto3>=1.35,<2"

# Copy all four C++ worker binaries from the builder stage. Each is linked
# against ONLY its product's libs and has a per-product INSTALL_RPATH, so
# their address spaces stay independent at load time.
COPY --from=builder /build/build/office-convert-worker-docx  /usr/local/bin/office-convert-worker-docx
COPY --from=builder /build/build/office-convert-worker-pptx  /usr/local/bin/office-convert-worker-pptx
COPY --from=builder /build/build/office-convert-worker-xlsx  /usr/local/bin/office-convert-worker-xlsx
COPY --from=builder /build/build/office-convert-worker-pdf   /usr/local/bin/office-convert-worker-pdf
COPY --from=builder /build/build/office-convert-worker-email /usr/local/bin/office-convert-worker-email
RUN chmod 755 /usr/local/bin/office-convert-worker-*

# Copy each product's .so trees to /opt/aspose/<Product>/, matching the
# builder layout that the binaries' RPATHs reference. Each product's own
# CodePorting framework is now restored (the previous single-binary build
# dropped Words' 26.3 cs2cpp in favor of Slides' 26.4; with per-binary
# isolation, each product gets its matching framework version back).
COPY --from=builder /opt/aspose/Words/Aspose.Words.Cpp/lib                            /opt/aspose/Words/Aspose.Words.Cpp/lib
COPY --from=builder /opt/aspose/Words/Aspose.Words.Shaping.HarfBuzz.Cpp/lib           /opt/aspose/Words/Aspose.Words.Shaping.HarfBuzz.Cpp/lib
COPY --from=builder /opt/aspose/Words/CodePorting.Translator.Cs2Cpp.Framework/lib     /opt/aspose/Words/CodePorting.Translator.Cs2Cpp.Framework/lib
COPY --from=builder /opt/aspose/Cells/Aspose.Cells/lib                                /opt/aspose/Cells/Aspose.Cells/lib
COPY --from=builder /opt/aspose/Slides/Aspose.Slides.Cpp/lib                          /opt/aspose/Slides/Aspose.Slides.Cpp/lib
COPY --from=builder /opt/aspose/Slides/CodePorting.Translator.Cs2Cpp.Framework/lib    /opt/aspose/Slides/CodePorting.Translator.Cs2Cpp.Framework/lib
COPY --from=builder /opt/aspose/PDF/lib                                                /opt/aspose/PDF/lib
COPY --from=builder /opt/aspose/Email/lib                                              /opt/aspose/Email/lib

# Copy the Python package.
COPY office_convert/ /app/office_convert/

# Runtime configuration defaults (operator overrides via -e).
ENV OFFICE_CONVERT_LICENSE_PATH=/aspose/license.lic \
    OFFICE_CONVERT_WORKER_BINARY_PREFIX=/usr/local/bin/office-convert-worker \
    OFFICE_CONVERT_ASPOSE_LIB_DIR=/opt/aspose \
    OFFICE_CONVERT_SCRATCH_DIR=/tmp/office-convert \
    OFFICE_CONVERT_LOG_FORMAT=json \
    OFFICE_CONVERT_LOG_LEVEL=info \
    OFFICE_CONVERT_MAX_JOBS=1 \
    OFFICE_CONVERT_PARALLEL=2 \
    OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS=300 \
    OFFICE_CONVERT_MAX_INPUT_BYTES=1073741824 \
    OFFICE_CONVERT_WORKER_RAM_BYTES=4294967296

EXPOSE 8080

# Create cache directory with appuser ownership so the named volume mount
# inherits the correct permissions on first use.
RUN mkdir -p /cache && chown appuser:appgroup /cache

USER appuser
WORKDIR /app

CMD ["uvicorn", "office_convert.server:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-keep-alive", "60", \
     "--timeout-graceful-shutdown", "900", \
     "--no-access-log"]
