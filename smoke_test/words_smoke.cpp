// Smoke test: Aspose.Words for C++ 26.3 + Total-for-C++ umbrella license on
// Linux x86_64. Run via `make smoke-words` from the project root.
//
// Validates:
//   1. SetLicense() accepts the bind-mounted .lic file (the Total umbrella
//      <Product>Aspose.Total for C++</Product> covers the standalone Words
//      library — license validator must say yes).
//   2. libAspose.Words.Cpp.so loads on Debian 12 (glibc 2.36 satisfies the
//      GLIBC_2.34 / GLIBCXX_3.4.30 floor reported by `readelf` on the .so).
//   3. Document round-trip through Save() produces non-watermarked PDF
//      (visual inspection — Aspose evaluation mode silently watermarks).
//
// Pre-integration test, NOT a regression test. Delete `smoke_test/` after
// the production Dockerfile + CMakeLists.txt refactor lands and the real
// integration tests pass.
//
// Exit codes:
//   0 = pass
//   1 = exception thrown during license/build/save (cause printed to stderr)
//   2 = usage error
//
// Usage: words_smoke <license-path> <output-pdf-path>

#if defined(__GNUC__)
#include <chrono>
#include <thread>
#include <Aspose.Words.Cpp/AsposeWordsLibrary.h>
#endif

#include <Aspose.Words.Cpp/Document.h>
#include <Aspose.Words.Cpp/DocumentBuilder.h>
#include <Aspose.Words.Cpp/Licensing/License.h>

#include <system/string.h>
#include <system/object.h>

#include <iostream>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "Usage: " << argv[0] << " <license-path> <output-pdf-path>\n";
        return 2;
    }

    try {
#if defined(__GNUC__)
        // Pthread workaround per Aspose quickstart
        // (https://gcc.gnu.org/bugzilla/show_bug.cgi?id=60662).
        std::this_thread::sleep_for(std::chrono::milliseconds{1});
#endif

        using namespace Aspose::Words;
        const System::String license_path(argv[1]);
        const System::String output_path(argv[2]);

        std::cout << "[1/3] Applying Aspose.Total-for-C++ license\n";
        auto license = System::MakeObject<License>();
        license->SetLicense(license_path);
        std::cout << "      OK\n";

        std::cout << "[2/3] Building minimal in-memory document\n";
        auto doc = System::MakeObject<Document>();
        auto builder = System::MakeObject<DocumentBuilder>(doc);
        builder->Writeln(u"Aspose.Words for C++ 26.3 — smoke test");
        builder->Writeln(u"License umbrella: Aspose.Total for C++");
        builder->Writeln(u"Runtime base: Debian 12 (glibc 2.36)");
        builder->Writeln(u"");
        builder->Writeln(u"If this PDF has no watermark, the license is fully active.");
        builder->Writeln(u"If you see 'Evaluation Only. Created with Aspose.Words...'");
        builder->Writeln(u"the license activation silently fell back to evaluation mode.");
        std::cout << "      OK\n";

        std::cout << "[3/3] Saving as PDF (extension-inferred format)\n";
        doc->Save(output_path);
        std::cout << "      OK\n\n";

        std::cout << "[PASS] Smoke test complete.\n"
                  << "       Inspect " << argv[2] << " — watermark presence = FAIL\n";

#if defined(__GNUC__)
        // Unload thread created by Aspose.Words for C++.
        Aspose::Words::AsposeWordsLibrary::PrepareForUnload();
#endif
        return 0;
    } catch (...) {
        std::cerr << "\n[FAIL] Exception thrown during smoke test.\n"
                  << "       Common causes (in order of frequency):\n"
                  << "       1. License file not found at " << argv[1] << "\n"
                  << "       2. License expired (current temp expires 2026-06-08)\n"
                  << "       3. License SKU does not cover Aspose.Words\n"
                  << "       4. glibc < 2.34 (need Debian 12 / Ubuntu 22.04+)\n"
                  << "       5. libAspose.Words.Cpp.so not on RUNPATH or LD_LIBRARY_PATH\n"
                  << "       6. libfontconfig1 missing from runtime image\n";
        return 1;
    }
}
