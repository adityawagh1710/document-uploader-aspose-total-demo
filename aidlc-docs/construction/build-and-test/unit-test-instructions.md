# Unit Test Execution — office-converter (Local v1)

## Test Inventory

| File | Subject | Type |
| ---- | ------- | ---- |
| `tests/unit/test_config.py` | `Settings` validation, env-var overrides | Unit |
| `tests/unit/test_logging.py` | JSON formatter, RequestIdFilter, ContextVar propagation | Unit |
| `tests/unit/test_license.py` | XML expiry parsing, state classification, refresh | Unit |
| `tests/unit/test_chunk_planner.py` | Hybrid split, seam balance, subdivision (example-based) | Unit |
| `tests/unit/test_cache.py` | Atomic-write, key layout, version namespacing | Unit |
| `tests/unit/test_qpdf.py` | Real qpdf binary streaming concat | Unit (needs qpdf) |
| `tests/unit/test_probe.py` | Magic-byte format detection, probe JSON parse | Unit |
| `tests/unit/test_aspose_worker.py` | Fake-subprocess exit-code translation, timeout | Unit |
| `tests/unit/test_orchestrator.py` | End-to-end with mocked aspose_worker (needs qpdf) | Unit |
| `tests/property/test_chunk_planner_pbt.py` | PBT, 500 examples | Property-based |
| `tests/property/test_subdivision_pbt.py` | PBT, 100 examples | Property-based |
| `tests/property/test_qpdf_concat_pbt.py` | PBT, 100 examples (needs qpdf + reportlab) | Property-based |
| `tests/property/test_format_detection_pbt.py` | PBT, 100 examples | Property-based |

## Prerequisites

```bash
uv sync                                  # creates .venv with [dev] extras
# Optionally generate corpus fixtures (needs python-docx + python-pptx + openpyxl from [dev]):
python -m tests.corpus._generate
# qpdf binary required for qpdf-dependent tests:
sudo apt-get install qpdf
```

## Run Unit + Property Tests

### Everything (fast, no Docker)

```bash
pytest tests/unit tests/property -v
```

### Filtered by file / marker

```bash
pytest tests/unit/test_chunk_planner.py -v
pytest tests/property -v                 # property-based only
pytest -k "not qpdf" tests/              # skip tests that need qpdf binary
```

### With coverage gate

```bash
pytest --cov=office_convert --cov-report=term-missing --cov-fail-under=80 \
    tests/unit tests/property
```

The 80% gate is configured in `pyproject.toml` under `[tool.coverage.report]`.
Coverage excludes `office_convert/server.py` (HTTP framework wiring covered
by integration tests, not unit).

## Expected Results

| Category | Expected | Notes |
| -------- | -------- | ----- |
| Unit tests | All pass | ~80 tests across 9 files |
| Property tests | All pass | Hypothesis examples: 500 (planner), 100 (others) |
| Coverage on `office_convert/` (excl. `server.py`) | ≥ 80% | Gate enforced in CI |
| Wall time (cold) | < 60 s | Most time in PBT |
| Wall time (warm, hypothesis cache hit) | < 20 s | |

## Test Reports

| Output | Location |
| ------ | -------- |
| pytest output | stdout |
| Coverage HTML | `htmlcov/index.html` (run with `--cov-report=html`) |
| Coverage XML | `coverage.xml` (run with `--cov-report=xml`) for CI ingestion |
| Hypothesis DB | `.hypothesis/` — commit this to CI cache so failing seeds replay |

## If a Test Fails

1. **Hypothesis property failure**: copy the `@reproduce_failure(...)`
   decorator hypothesis prints into the test, push, get the green. Then
   debug locally with `pytest --hypothesis-seed=<value>`.
2. **`test_qpdf.py` or `test_qpdf_concat_pbt.py` skipped**: `qpdf` binary
   not on PATH; install via apt or marked `pytest.skip` is fine.
3. **`test_aspose_worker.py::test_render_chunk_timeout` flaky**: the test
   uses a real 1-second sleep; CI runners with high contention may timeout
   earlier. Bump the test's `timeout` or move to a faster runner.
4. **Coverage drops below 80%**: identify uncovered lines via
   `pytest --cov-report=html`; either add tests or update the exclude list
   in `pyproject.toml`'s `[tool.coverage.run].omit`.
