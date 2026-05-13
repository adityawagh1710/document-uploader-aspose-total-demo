# Build Instructions — office-converter (Local v1)

## Prerequisites

| Tool | Version | Purpose |
| ---- | ------- | ------- |
| **Docker** | 24.0+ | Build the container image; required for the canonical path |
| **Python** | 3.11.x | Required iff building Python deps locally for tests (not for the image) |
| **uv** | 0.5.* | Python package manager + lockfile (preferred over pip) |
| **gcc-12 + cmake 3.25+** | (auto-installed in builder stage) | C++ worker compile; only needed locally if building C++ outside Docker |
| **Aspose.Total C++ SDK** | matching the temp license SKU | Operator-supplied tarball `aspose-total-cpp.tar.gz` placed next to the Dockerfile |
| **Aspose.Total C++ Temporary License** | valid `.lic` | Bind-mounted at runtime, NOT baked into the image |

### System Requirements

- **OS**: Linux x86_64 (production). macOS via Docker Desktop amd64 emulation (dev only).
- **Memory**: 8 GB RAM minimum recommended for the host. Each worker subprocess
  is capped at 2 GB virtual address space via `prlimit`.
- **Disk**: 5 GB for the runtime image; another 5 GB for the builder stage layers
  during build. `/tmp` should be on real disk (not tmpfs) if the operator runs the
  container with the default scratch dir, or use the `--tmpfs /tmp` flag.

### Environment Variables (build time)

None required at build time — the Dockerfile passes `-DASPOSE_SDK=/opt/aspose-sdk`
to CMake automatically once the operator-supplied tarball is extracted. All
runtime config is via `OFFICE_CONVERT_*` env vars at `docker run` time
(documented in `README.md`).

## Build Steps

### 1. Place the Aspose SDK tarball in the build context

```bash
cp /path/to/aspose-total-cpp.tar.gz .
```

The Dockerfile's first builder-stage step is `COPY aspose-total-cpp.tar.gz /tmp/`.
Build fails immediately at the `COPY` if the file is missing — fast feedback.

### 2. Build the Docker image (canonical path)

```bash
docker build -t office-convert:dev .
```

**What happens (multi-stage build)**:

- **Stage 1 (builder)** — `debian:bookworm`:
  - `apt install gcc-12 g++-12 cmake make build-essential`
  - Extract Aspose tarball to `/opt/aspose-sdk/`
  - `COPY worker_cpp/`
  - `cmake -S worker_cpp -B build -DASPOSE_SDK=/opt/aspose-sdk -DCMAKE_BUILD_TYPE=Release`
  - `cmake --build build --target office-convert-worker -j$(nproc)`
  - Produces `/build/build/office-convert-worker`
- **Stage 2 (runtime)** — `python:3.11-slim-bookworm`:
  - `apt install qpdf util-linux libstdc++6 libgcc-s1`
  - Create non-root `appuser:appgroup`
  - `uv pip install` the Python deps
  - `COPY --from=builder` the worker binary into `/usr/local/bin/`
  - `COPY --from=builder` the Aspose `.so` files into `/usr/local/lib/aspose/`
  - `COPY office_convert/` the Python package
  - Set `LD_LIBRARY_PATH=/usr/local/lib/aspose` + all `OFFICE_CONVERT_*` defaults
  - `USER appuser`
  - `CMD ["uvicorn", ...]`

### 3. (Optional) Local Python build for tests outside Docker

Only needed if you want to run `pytest` against the Python code on the host:

```bash
uv sync                                  # creates .venv with [dev] extras
uv pip install -e .                      # editable install
# (Optionally also: uv pip install -e .[e2e] for the Testcontainers extra)
```

### 4. Verify Build Success

Expected output of `docker build`:

```
[+] Building 45.2s (24/24) FINISHED
 => => exporting layers ...
 => => writing image sha256:...
 => => naming to docker.io/library/office-convert:dev
```

Inspect the resulting image:

```bash
docker images office-convert:dev          # expect a single ~600 MB image
docker inspect office-convert:dev | grep -i user        # expect "User": "appuser"
docker run --rm office-convert:dev /usr/local/bin/office-convert-worker --help \
  2>&1 | head -5
```

Build artifacts:

- The runtime image only. There are no separately distributed wheel files for v1.
- The builder stage's intermediate artifacts (`/build/build/office-convert-worker`,
  `/opt/aspose-sdk/`) are NOT in the runtime image; multi-stage `COPY --from=builder`
  cherry-picks only the binary and `.so` files.

## Common Build Failures

### Failure: `aspose-total-cpp.tar.gz: No such file or directory`

- **Cause**: Tarball not placed in build context.
- **Fix**: `cp /path/to/aspose-total-cpp.tar.gz .` then rebuild.

### Failure: CMake error `ASPOSE_SDK not defined` (warning only)

- **Cause**: Building outside Docker without `-DASPOSE_SDK`.
- **Effect**: Worker still compiles, but format render functions throw
  `RenderException("SDK not linked in this build")` at runtime.
- **Fix**: For real conversion, build via the Dockerfile (which sets
  `-DASPOSE_SDK=/opt/aspose-sdk`).

### Failure: linker errors `undefined reference to Aspose::...`

- **Cause**: SDK tarball does not contain the expected `.so` files at
  `/opt/aspose-sdk/lib/lib*.so`, or library names differ from those listed in
  `worker_cpp/CMakeLists.txt` (`aspose_words`, `aspose_slides`, `aspose_cells`,
  `aspose_pdf`).
- **Fix**: Inspect the extracted tarball:
  `docker run --rm -it debian:bookworm bash -c "tar -xzf - && ls aspose-sdk/lib/"`
  and update the `target_link_libraries(...)` call in `worker_cpp/CMakeLists.txt`
  to match the actual SDK filenames.

### Failure: `python:3.11-slim-bookworm` pull rate-limited

- **Cause**: Anonymous Docker Hub pull rate limits.
- **Fix**: `docker login` with a Hub account, or mirror the base image to your
  own registry.

### Failure: `apt-get install qpdf` fails in runtime stage

- **Cause**: Build host can't reach Debian's apt repos (network restrictions).
- **Fix**: Use a Debian mirror; configure `APT_HTTP_PROXY` build arg if needed.
