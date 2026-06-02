// Package cache is a filesystem cache for final and per-chunk PDFs.
//
// Ported from office_convert/cache.py. Implements FR-7. Keys include the Aspose
// version so version upgrades naturally invalidate cached entries. Atomic write
// protocol per business-rules.md §7 and nfr-design-patterns.md §6.
//
// When cacheDir is "" the cache is disabled (all get/put are no-ops).
package cache

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
)

// Manager is a content-addressable cache keyed by
// <aspose_version>/<final|chunks>/<sha>.pdf.
type Manager struct {
	dir           string // "" == disabled
	asposeVersion string
}

// NewManager constructs a cache. If dir != "", the final/ and chunks/
// subdirectories are created eagerly (matching cache.py's __init__).
func NewManager(dir, asposeVersion string) (*Manager, error) {
	m := &Manager{dir: dir, asposeVersion: asposeVersion}
	if dir != "" {
		for _, sub := range []string{"final", "chunks"} {
			if err := os.MkdirAll(filepath.Join(dir, asposeVersion, sub), 0o755); err != nil {
				return nil, err
			}
		}
	}
	return m, nil
}

// Enabled reports whether a cache directory is configured.
func (m *Manager) Enabled() bool { return m.dir != "" }

func (m *Manager) finalPath(sourceSHA256 string) string {
	return filepath.Join(m.dir, m.asposeVersion, "final", sourceSHA256+".pdf")
}

func (m *Manager) chunkPath(chunkSHA256 string) string {
	return filepath.Join(m.dir, m.asposeVersion, "chunks", chunkSHA256+".pdf")
}

// GetFinal returns the cached final-PDF path, or "" if absent/disabled.
func (m *Manager) GetFinal(sourceSHA256 string) string {
	if !m.Enabled() {
		return ""
	}
	p := m.finalPath(sourceSHA256)
	if fileExists(p) {
		return p
	}
	return ""
}

// PutFinal atomically stores a final PDF.
func (m *Manager) PutFinal(sourceSHA256, pdfPath string) error {
	if !m.Enabled() {
		return nil
	}
	return AtomicWrite(m.finalPath(sourceSHA256), pdfPath)
}

// GetChunk returns the cached chunk-PDF path, or "" if absent/disabled.
func (m *Manager) GetChunk(chunkSHA256 string) string {
	if !m.Enabled() {
		return ""
	}
	p := m.chunkPath(chunkSHA256)
	if fileExists(p) {
		return p
	}
	return ""
}

// PutChunk atomically stores a chunk PDF.
func (m *Manager) PutChunk(chunkSHA256, pdfPath string) error {
	if !m.Enabled() {
		return nil
	}
	return AtomicWrite(m.chunkPath(chunkSHA256), pdfPath)
}

// FinalTempPath returns a temp path for tee-to-cache during streaming merge.
// The caller writes to this path while streaming, then calls FinalizeFinal on
// success. Returns "" if the cache is disabled. The parent directory is ensured
// to exist so the caller can open the path for write immediately.
func (m *Manager) FinalTempPath(sourceSHA256 string) (string, error) {
	if !m.Enabled() {
		return "", nil
	}
	target := m.finalPath(sourceSHA256)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return "", err
	}
	return tempName(target), nil
}

// ClearResult mirrors the dict returned by cache.py CacheManager.clear().
type ClearResult struct {
	Enabled      bool  `json:"enabled"`
	FilesDeleted int   `json:"files_deleted"`
	BytesFreed   int64 `json:"bytes_freed"`
	Errors       int   `json:"errors,omitempty"`
}

// Clear wipes the cache directory's contents and reports what was freed.
// Individual unlink errors are swallowed and counted so a partial wipe still
// completes.
func (m *Manager) Clear() ClearResult {
	if !m.Enabled() {
		return ClearResult{Enabled: false}
	}
	res := ClearResult{Enabled: true}
	// Walk once for sizing + unlink, then prune empty dirs bottom-up.
	var dirs []string
	_ = filepath.Walk(m.dir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			res.Errors++
			return nil
		}
		if info.IsDir() {
			if p != m.dir {
				dirs = append(dirs, p)
			}
			return nil
		}
		res.BytesFreed += info.Size()
		if err := os.Remove(p); err != nil {
			res.Errors++
			res.BytesFreed -= info.Size()
		} else {
			res.FilesDeleted++
		}
		return nil
	})
	// Remove deepest directories first.
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Remove(dirs[i]) // best-effort; non-empty dirs silently skipped
	}
	return res
}

// FinalizeFinal atomically renames a successfully-written temp file into the
// cache. If the cache is disabled, the temp file is removed.
func (m *Manager) FinalizeFinal(sourceSHA256, tempPath string) error {
	if !m.Enabled() {
		if err := os.Remove(tempPath); err != nil && !os.IsNotExist(err) {
			slog.Warn("failed to remove temp file", "path", tempPath, "err", err)
		}
		return nil
	}
	target := m.finalPath(sourceSHA256)
	// Ensure durability before the visibility-flipping rename.
	if f, err := os.Open(tempPath); err == nil {
		_ = f.Sync()
		_ = f.Close()
	} else {
		slog.Warn("fsync open failed", "path", tempPath, "err", err)
	}
	return os.Rename(tempPath, target)
}

// AtomicWrite copies source to target atomically via temp file + rename.
// POSIX rename is atomic within a single filesystem; readers see either the old
// file or the new file, never a partial one.
func AtomicWrite(target, source string) (err error) {
	if mkErr := os.MkdirAll(filepath.Dir(target), 0o755); mkErr != nil {
		return mkErr
	}
	tmp := tempName(target)
	defer func() {
		if err != nil {
			_ = os.Remove(tmp)
		}
	}()
	if err = copyFile(source, tmp); err != nil {
		return err
	}
	if f, openErr := os.Open(tmp); openErr == nil {
		_ = f.Sync()
		_ = f.Close()
	}
	return os.Rename(tmp, target)
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// tempName mirrors cache.py's "<target>.tmp.<pid>.<uuidhex>" scheme.
func tempName(target string) string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return fmt.Sprintf("%s.tmp.%d.%s", target, os.Getpid(), hex.EncodeToString(b[:]))
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
