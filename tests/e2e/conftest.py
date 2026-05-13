"""End-to-end test fixtures using Testcontainers.

These tests build a real container, bind-mount a real Aspose license, and
exercise /convert over real HTTP. They catch what in-process integration
tests (FastAPI TestClient + fake worker) cannot:

- Dockerfile bugs (apt deps, env vars, CMD, USER, LD_LIBRARY_PATH)
- Real C++ worker binary linkage (Aspose .so symbols resolvable)
- Real Aspose render + license activation
- Real qpdf concat at real sizes
- Real prlimit RLIMIT_AS=2G behavior under OOM

Gating: tests are SKIPPED unless `OFFICE_CONVERT_E2E_LICENSE` is set to the
path of a valid Aspose.Total C++ temporary license. The Docker image must
also have been built with the real Aspose SDK in the build context.

Run:
    OFFICE_CONVERT_E2E_LICENSE=/path/to/license.lic \\
    OFFICE_CONVERT_E2E_IMAGE=office-convert:test \\
    pytest tests/e2e -m e2e
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from pathlib import Path

import pytest

# Skip gracefully if testcontainers / httpx not installed (e.g. CI without the
# e2e optional extra). The marker still works; the fixtures just error if
# someone tries to use them.
try:
    import httpx
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    _HAS_E2E_DEPS = True
except ImportError:
    _HAS_E2E_DEPS = False


E2E_LICENSE_ENV = "OFFICE_CONVERT_E2E_LICENSE"
E2E_IMAGE_ENV = "OFFICE_CONVERT_E2E_IMAGE"
DEFAULT_IMAGE = "office-convert:test"

_skip_reason = (
    "set OFFICE_CONVERT_E2E_LICENSE=<path-to-.lic> to enable e2e tests; "
    "requires Docker daemon, the office-convert image built with a real "
    "Aspose.Total C++ SDK, and a valid Aspose.Total C++ Temporary License."
)

e2e = pytest.mark.skipif(
    not _HAS_E2E_DEPS or not os.environ.get(E2E_LICENSE_ENV),
    reason=_skip_reason,
)


@pytest.fixture(scope="session")
def e2e_license_path() -> Path:
    raw = os.environ.get(E2E_LICENSE_ENV)
    if not raw:
        pytest.skip(_skip_reason)
    path = Path(raw)
    if not path.is_file():
        pytest.skip(f"{E2E_LICENSE_ENV}={raw} is not a readable file")
    return path


@pytest.fixture(scope="session")
def e2e_image() -> str:
    return os.environ.get(E2E_IMAGE_ENV, DEFAULT_IMAGE)


@pytest.fixture(scope="session")
def converter(e2e_image: str, e2e_license_path: Path) -> Generator[object, None, None]:
    """Bring up the office-convert container once per test session.

    Uses bind-mount for the Aspose license. Tests pass if the container
    starts and responds; e2e tests beyond that exercise the conversion
    pipeline.
    """
    if not _HAS_E2E_DEPS:
        pytest.skip("testcontainers and httpx required; install via `uv pip install -e .[dev]`")

    container = (
        DockerContainer(e2e_image)
        .with_exposed_ports(8080)
        .with_volume_mapping(
            str(e2e_license_path.resolve()),
            "/aspose/license.lic",
            "ro",
        )
        # tmpfs scratch (matches the recommended production posture)
        .with_command(["--read-only=false"])  # placeholder if customizing CMD
    )
    container.start()
    try:
        # Wait for the server to log its server_start event.
        wait_for_logs(container, "server_start", timeout=60)
        # Give uvicorn a beat to bind after the lifespan hook fires.
        time.sleep(0.5)
        yield container
    finally:
        container.stop()


@pytest.fixture
def base_url(converter: object) -> str:
    """Compute http://<host>:<port> for the running container."""
    if not _HAS_E2E_DEPS:
        pytest.skip("testcontainers required")
    host = converter.get_container_host_ip()  # type: ignore[attr-defined]
    port = converter.get_exposed_port(8080)  # type: ignore[attr-defined]
    return f"http://{host}:{port}"


@pytest.fixture
def http_client() -> Generator[object, None, None]:
    """A shared httpx.Client with a generous timeout for conversions."""
    if not _HAS_E2E_DEPS:
        pytest.skip("httpx required")
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=900, write=60, pool=10)) as client:
        yield client
