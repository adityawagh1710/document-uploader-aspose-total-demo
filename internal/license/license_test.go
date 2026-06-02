package license

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

func TestClassifyThresholds(t *testing.T) {
	cases := []struct {
		days int
		has  bool
		want types.LicenseState
	}{
		{0, false, types.LicenseStatePermanent}, // no expiry
		{100, true, types.LicenseStateHealthy},
		{8, true, types.LicenseStateHealthy},
		{7, true, types.LicenseStateWarn}, // not strictly > 7
		{4, true, types.LicenseStateWarn},
		{3, true, types.LicenseStateCritical},
		{1, true, types.LicenseStateCritical},
		{0, true, types.LicenseStateExpiringToday},
		{-1, true, types.LicenseStateExpired},
	}
	for _, c := range cases {
		if got := Classify(c.days, c.has); got != c.want {
			t.Errorf("Classify(%d, %v) = %q, want %q", c.days, c.has, got, c.want)
		}
	}
}

func TestParseExpiryNumericAndPermanent(t *testing.T) {
	dir := t.TempDir()

	// Numeric YYYYMMDD expiry.
	lic := filepath.Join(dir, "dated.lic")
	mustWrite(t, lic, `<License><Data><SubscriptionExpiry>20991231</SubscriptionExpiry></Data></License>`)
	exp, has, err := parseExpiry(lic)
	if err != nil || !has {
		t.Fatalf("parseExpiry dated: has=%v err=%v", has, err)
	}
	if exp.Year() != 2099 || exp.Month() != 12 || exp.Day() != 31 {
		t.Fatalf("expiry = %v, want 2099-12-31", exp)
	}

	// Well-formed but no expiry field -> permanent.
	perm := filepath.Join(dir, "perm.lic")
	mustWrite(t, perm, `<License><Data><Product>Aspose.Total for C++</Product></Data></License>`)
	_, has, err = parseExpiry(perm)
	if err != nil {
		t.Fatalf("parseExpiry perm err=%v", err)
	}
	if has {
		t.Fatal("permanent license should report no expiry")
	}
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
