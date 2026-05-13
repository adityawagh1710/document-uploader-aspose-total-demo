# Smoke test — Aspose.Words for C++ 26.3 license activation

Pre-integration Docker-based experiment. Validates the 4-libs Aspose vendor
path **before** refactoring the production Dockerfile and `worker_cpp/CMakeLists.txt`.

## What it validates

1. The existing `Aspose.TotalforC++.lic` (`<Product>Aspose.Total for C++</Product>`
   umbrella) unlocks Aspose.Words for C++ 26.3 downloaded as a standalone library
   (not bundled inside the Linux Total ZIP).
2. `libAspose.Words.Cpp.so` (extracted from the Windows Total ZIP's inner
   universal Words package) loads on Debian 12's glibc 2.36, satisfying the
   `GLIBC_2.34` / `GLIBCXX_3.4.30` requirements from `readelf`.
3. A round-trip through `Document::Save(*.pdf)` produces non-watermarked output.
   Watermark presence indicates Aspose silently fell back to evaluation mode.

## What it does NOT validate

- Cells / Slides / PDF library activation (separately or together).
- Subprocess + `prlimit` 2 GB ceiling enforcement.
- Chunk planning, qpdf concat, FastAPI orchestrator, cache, license-expiry
  warnings — anything other than Aspose.Words + license.

## Run it

From the project root:

```bash
make smoke-words
```

Pass criteria:

- Process exits with code `0`.
- `/tmp/oc-smoke/words_smoke.pdf` exists and is a valid PDF.
- Opening the PDF shows the test text **without an Aspose evaluation watermark**
  (the watermark would say "Evaluation Only. Created with Aspose.Words for C++...").

## Failure modes

| Symptom                                                | Likely cause                                           |
| ------------------------------------------------------ | ------------------------------------------------------ |
| `[FAIL] Exception thrown during smoke test.`           | One of the 6 causes listed in the stderr block.        |
| `version 'GLIBC_2.34' not found`                       | Base image's glibc < 2.34 (we use bookworm = 2.36 ✓).  |
| `error while loading shared libraries: libAspose...`   | RUNPATH issue inside the built binary.                 |
| PDF has "Evaluation Only" watermark on every page      | License activation silently fell back; SKU mismatch?   |
| `could not initialize fontconfig`                      | `libfontconfig1` missing from runtime image.           |
| `CMake Error: ... Aspose.Words.Cpp ... NOT_FOUND`      | `vendor/aspose/Words/` empty or incomplete.            |

## When to delete this

Once the production Dockerfile / `worker_cpp/CMakeLists.txt` refactor lands and
the full test suite (`make qa`) passes against the real 4-libs vendor build,
this `smoke_test/` directory can be removed.

## Files

- `words_smoke.cpp` — minimal C++ program: license → build doc → save PDF.
- `CMakeLists.txt` — adapted from Aspose's own `quickstart/CMakeLists.txt`
  (shipped inside `Aspose.Words.Cpp_26.3.zip`).
- `Dockerfile.smoke` — single-stage Debian 12 image (intentionally simpler
  than the production multi-stage Dockerfile).
- This README.
