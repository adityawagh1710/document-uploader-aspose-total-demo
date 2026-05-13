# Integration Test Instructions — office-converter (Local v1)

## Purpose

In-process integration tests via FastAPI's `TestClient`. Exercise the
HTTP layer and the orchestrator's coordination logic against a **fake
worker stand-in** (see `tests/conftest.py::fake_worker_script`).
Coverage stops at the subprocess boundary — Aspose itself is not
called. For real-Aspose end-to-end testing see
`e2e-test-instructions.md`.

## Test Inventory

| File | Targets |
| ---- | ------- |
| `tests/integration/test_convert_endpoint.py` | `POST /convert` happy path, format rejection, size-limit rejection, X-Request-ID correlation |
| `tests/integration/test_health_endpoint.py` | `GET /health` ready / not-ready / license days remaining |

## Test Scenarios

### Scenario 1: `POST /convert` happy path

- **Description**: Upload a small PDF, receive a streamed PDF response.
- **Setup**: `conftest.py::client` fixture builds an app with a fake
  worker (Python script that emits canned PDFs via ReportLab).
- **Test Steps**:
  1. `client.post("/convert", files={"file": (..., pdf_bytes, ...)})`
  2. Assert `200` + `Content-Type: application/pdf` + `X-Request-ID`
     header + body starts with `%PDF-`
- **Expected**: PDF round-trips with HTTP metadata headers populated.
- **Cleanup**: TestClient context-manager exits; scratch dir under
  `tmp_path` GC'd.

### Scenario 2: Format detection rejects PNG before disk write

- **Description**: Magic-byte detection on the first 512 bytes rejects
  unsupported formats before buffering the body.
- **Test Steps**:
  1. POST a PNG body
  2. Assert `400` + `failure_class: unsupported_format`
- **Expected**: Rejection happens before the body is buffered to scratch.

### Scenario 3: Size-limit rejection

- **Description**: A request body exceeding `max_input_bytes` is rejected
  incrementally (not after full buffering).
- **Test Steps**:
  1. Build an app with `max_input_bytes=1 MB`
  2. POST a 1 MB + 10-byte body
  3. Assert `400` + `failure_class: input_too_large`

### Scenario 4: `GET /health` reports days_remaining

- **Description**: With a valid license expiring in N days, `/health`
  returns N.
- **Test Steps**:
  1. Build app with a license that expires in 20 days
  2. GET `/health`
  3. Assert `license_days_remaining in (19, 20)` (clock-drift tolerant)

### Scenario 5: Expired license flips /health to not-ready

- **Description**: An expired license must surface as `ready: false`
  and `problems` containing `"license_expired"`.

## Setup

```bash
uv sync                                  # [dev] deps
sudo apt-get install qpdf                # required for orchestrator integration
```

No real Docker, no real Aspose, no real network port. Tests use
FastAPI's `TestClient` which mounts the ASGI app in-process.

## Run

### Everything

```bash
pytest tests/integration -v
```

### One file

```bash
pytest tests/integration/test_convert_endpoint.py -v
```

### With orchestrator coverage

```bash
pytest --cov=office_convert tests/integration/
```

## Expected Results

| Item | Expected |
| ---- | -------- |
| Tests passed | ~8 across 2 files |
| Wall time | < 5 s (in-process) |
| Skipped tests on hosts without qpdf | `test_convert_returns_pdf` family |

## Cleanup

Automatic — `TestClient` cleanup runs as a context manager; `tmp_path`
fixture removes scratch dirs after each test. No persistent state.

## What Integration Tests Do NOT Cover

- The actual Dockerfile (use e2e suite)
- The real C++ worker binary (use e2e suite)
- The real Aspose rendering (use e2e suite + real license)
- The real `prlimit RLIMIT_AS=2G` enforcement (use e2e suite)
- qpdf at real PDF sizes (use e2e suite)

For those, see `e2e-test-instructions.md`.
