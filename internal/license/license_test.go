package license

import (
	"os"
	"path/filepath"
	"testing"
	"time"

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

// When both LicenseExpiry and SubscriptionExpiry are present, the EARLIER one
// binds (it's when Aspose actually stops rendering). Mirrors the real temp
// license: LicenseExpiry 2026-06-08 (past) vs SubscriptionExpiry 2027-05-08.
func TestParseExpiryPrefersEarliestBindingDate(t *testing.T) {
	dir := t.TempDir()

	lic := filepath.Join(dir, "both.lic")
	mustWrite(t, lic, `<License><Data>`+
		`<SubscriptionExpiry>20270508</SubscriptionExpiry>`+
		`<LicenseExpiry>20260608</LicenseExpiry>`+
		`</Data></License>`)

	exp, has, err := parseExpiry(lic)
	require.NoError(t, err)
	require.True(t, has)
	// Earliest = LicenseExpiry 2026-06-08, NOT SubscriptionExpiry 2027.
	assert.Equal(t, 2026, exp.Year())
	assert.Equal(t, "June", exp.Month().String())
	assert.Equal(t, 8, exp.Day())

	// A manager over this file is expired (the binding date is in the past).
	m := NewManager(lic)
	expired, err := m.IsExpired()
	require.NoError(t, err)
	assert.True(t, expired, "past LicenseExpiry must make the license expired")
}

// Order-independence: LicenseExpiry listed first still yields the earliest.
func TestParseExpiryEarliestRegardlessOfOrder(t *testing.T) {
	dir := t.TempDir()
	lic := filepath.Join(dir, "ordered.lic")
	mustWrite(t, lic, `<License><Data>`+
		`<LicenseExpiry>20300101</LicenseExpiry>`+
		`<SubscriptionExpiry>20250101</SubscriptionExpiry>`+
		`</Data></License>`)
	exp, has, err := parseExpiry(lic)
	require.NoError(t, err)
	require.True(t, has)
	assert.Equal(t, 2025, exp.Year(), "earliest of the two binds regardless of element order")
}

// A license renewed in place (file rewritten) must be picked up WITHOUT a
// restart — the manager re-parses when the file's mtime changes.
func TestManagerAutoRefreshesOnFileChange(t *testing.T) {
	dir := t.TempDir()
	lic := filepath.Join(dir, "renewed.lic")

	// Start expired (past LicenseExpiry).
	mustWrite(t, lic, `<License><Data><LicenseExpiry>20200101</LicenseExpiry></Data></License>`)
	older := time.Now().Add(-2 * time.Hour)
	require.NoError(t, os.Chtimes(lic, older, older))

	m := NewManager(lic)
	expired, err := m.IsExpired()
	require.NoError(t, err)
	require.True(t, expired, "past-dated license should be expired")

	// Operator renews in place → future expiry, with a newer mtime.
	mustWrite(t, lic, `<License><Data><LicenseExpiry>20990101</LicenseExpiry></Data></License>`)
	newer := time.Now()
	require.NoError(t, os.Chtimes(lic, newer, newer))

	expired, err = m.IsExpired()
	require.NoError(t, err)
	assert.False(t, expired, "renewed license must be picked up without Refresh() or restart")
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	require.NoError(t, os.WriteFile(path, []byte(content), 0o644))
}
