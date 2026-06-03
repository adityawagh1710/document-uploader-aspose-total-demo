package license

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

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
		assert.Equalf(t, c.want, Classify(c.days, c.has), "Classify(%d, %v)", c.days, c.has)
	}
}

func TestParseExpiryNumericAndPermanent(t *testing.T) {
	dir := t.TempDir()

	// Numeric YYYYMMDD expiry.
	lic := filepath.Join(dir, "dated.lic")
	mustWrite(t, lic, `<License><Data><SubscriptionExpiry>20991231</SubscriptionExpiry></Data></License>`)
	exp, has, err := parseExpiry(lic)
	require.NoError(t, err)
	require.True(t, has, "dated license should report an expiry")
	assert.Equal(t, 2099, exp.Year())
	assert.Equal(t, "December", exp.Month().String())
	assert.Equal(t, 31, exp.Day())

	// Well-formed but no expiry field -> permanent.
	perm := filepath.Join(dir, "perm.lic")
	mustWrite(t, perm, `<License><Data><Product>Aspose.Total for C++</Product></Data></License>`)
	_, has, err = parseExpiry(perm)
	require.NoError(t, err)
	require.False(t, has, "permanent license should report no expiry")
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	require.NoError(t, os.WriteFile(path, []byte(content), 0o644))
}
