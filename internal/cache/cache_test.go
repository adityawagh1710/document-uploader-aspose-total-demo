package cache

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestDisabledCacheIsNoOp(t *testing.T) {
	m, err := NewManager("", "v1")
	require.NoError(t, err)
	require.False(t, m.Enabled(), "empty dir should be disabled")
	require.Empty(t, m.GetFinal("abc"), "disabled GetFinal should be empty")
	require.NoError(t, m.PutFinal("abc", "/nonexistent"), "disabled PutFinal should be a no-op")
	require.False(t, m.Clear().Enabled, "disabled Clear should report Enabled=false")
}

func TestFinalRoundTripAndClear(t *testing.T) {
	dir := t.TempDir()
	m, err := NewManager(dir, "26.4")
	require.NoError(t, err)
	require.True(t, m.Enabled(), "should be enabled")

	src := filepath.Join(dir, "src.pdf")
	require.NoError(t, os.WriteFile(src, []byte("%PDF-1.7 hello"), 0o644))

	sha := "deadbeef"
	require.Empty(t, m.GetFinal(sha), "miss expected before PutFinal")
	require.NoError(t, m.PutFinal(sha, src))
	hit := m.GetFinal(sha)
	require.NotEmpty(t, hit, "expected cache hit after PutFinal")
	got, err := os.ReadFile(hit)
	require.NoError(t, err)
	require.Equal(t, "%PDF-1.7 hello", string(got))

	// Version namespacing: a different version must miss.
	m2, _ := NewManager(dir, "26.3")
	require.Empty(t, m2.GetFinal(sha), "different aspose version should not hit")

	res := m.Clear()
	require.True(t, res.Enabled)
	require.NotZero(t, res.FilesDeleted)
	require.NotZero(t, res.BytesFreed)
	require.Empty(t, m.GetFinal(sha), "entry should be gone after Clear")
}

func TestFinalTempPathAndFinalize(t *testing.T) {
	dir := t.TempDir()
	m, _ := NewManager(dir, "v1")
	tmp, err := m.FinalTempPath("abc123")
	require.NoError(t, err)
	require.NotEmpty(t, tmp)
	require.NoError(t, os.WriteFile(tmp, []byte("streamed"), 0o644))
	require.NoError(t, m.FinalizeFinal("abc123", tmp))
	require.NotEmpty(t, m.GetFinal("abc123"), "finalize should publish the entry")
}
