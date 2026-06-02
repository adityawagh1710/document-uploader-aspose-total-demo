// Package config holds runtime configuration via OFFICE_CONVERT_* env vars.
//
// Ported from office_convert/config.py. Implements NFR-8 (single source of
// truth for config). Validation rules per business-rules.md §12. Failures
// surface at server startup (Load returns an error), before serving any
// request — mirroring pydantic-settings validation-on-construction.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Settings holds all OFFICE_CONVERT_* configuration, loaded once at startup.
//
// Field defaults and the [min,max] validation bounds match config.py's
// pydantic Field(ge=..., le=...) declarations exactly.
type Settings struct {
	MaxJobs  int
	Parallel int

	CacheDir           string // "" == None (cache disabled)
	LicensePath        string
	ScratchDir         string
	AsposeLibDir       string
	WorkerBinaryPrefix string

	LogFormat string // "json" | "human"
	LogLevel  string // "debug" | "info" | "warn" | "error"

	ChunkTimeoutSeconds int
	MaxInputBytes       int64

	WorkerRAMBytes int64

	AsposeVersion string

	MaxPagesPerChunk     int
	MaxMBPerChunk        int
	XLSXMinPagesPerChunk int
	PPTXMinPagesPerChunk int

	PoolMinChunks   int
	XLSXMaxPoolSize int
	ForkAfterLoad   bool

	RateLimitEnabled  bool
	RateLimitPerIPRPM int
	RateLimitBurst    int
	RateLimitMaxKeys  int
	RateLimitTrustXFF bool

	// S3 source/sink integration.
	S3Enabled                bool
	S3Region                 string // "" == None
	S3InputBucketsAllowlist  string // "" == None
	S3OutputBucketsAllowlist string // "" == None
	S3DefaultOutputBucket    string // "" == None
	S3OutputKeyTemplate      string
	S3PresignTTLSeconds      int
	S3PublicEndpoint         string // "" == None
}

const envPrefix = "OFFICE_CONVERT_"

// Load reads OFFICE_CONVERT_* env vars, applies defaults, and validates bounds.
// It returns an error on the first validation failure (fail-fast at startup).
func Load() (*Settings, error) {
	var errs []string
	fail := func(field string, err error) {
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", field, err))
		}
	}

	s := &Settings{
		// Path/string defaults from config.py.
		LicensePath:         "/aspose/license.lic",
		ScratchDir:          "/tmp/office-convert",
		AsposeLibDir:        "/usr/local/lib/aspose",
		WorkerBinaryPrefix:  "/usr/local/bin/office-convert-worker",
		LogFormat:           "json",
		LogLevel:            "info",
		AsposeVersion:       "unknown",
		S3OutputKeyTemplate: "pdf/{request_id}.pdf",
	}

	var err error
	s.MaxJobs, err = envInt("MAX_JOBS", 1, 1, 10)
	fail("MAX_JOBS", err)
	s.Parallel, err = envInt("PARALLEL", 4, 1, 16)
	fail("PARALLEL", err)

	s.CacheDir = envStr("CACHE_DIR", "")
	s.LicensePath = envStr("LICENSE_PATH", s.LicensePath)
	s.ScratchDir = envStr("SCRATCH_DIR", s.ScratchDir)
	s.AsposeLibDir = envStr("ASPOSE_LIB_DIR", s.AsposeLibDir)
	s.WorkerBinaryPrefix = envStr("WORKER_BINARY_PREFIX", s.WorkerBinaryPrefix)

	s.LogFormat = envStr("LOG_FORMAT", s.LogFormat)
	fail("LOG_FORMAT", oneOf(s.LogFormat, "json", "human"))
	s.LogLevel = envStr("LOG_LEVEL", s.LogLevel)
	fail("LOG_LEVEL", oneOf(s.LogLevel, "debug", "info", "warn", "error"))

	s.ChunkTimeoutSeconds, err = envInt("CHUNK_TIMEOUT_SECONDS", 300, 30, 3600)
	fail("CHUNK_TIMEOUT_SECONDS", err)

	const mib = 1024 * 1024
	const gib = 1024 * mib
	s.MaxInputBytes, err = envInt64("MAX_INPUT_BYTES", gib, mib, gib)
	fail("MAX_INPUT_BYTES", err)

	s.WorkerRAMBytes, err = envInt64("WORKER_RAM_BYTES", 6*gib, 2*gib, 64*gib)
	fail("WORKER_RAM_BYTES", err)

	s.AsposeVersion = envStr("ASPOSE_VERSION", s.AsposeVersion)

	s.MaxPagesPerChunk, err = envInt("MAX_PAGES_PER_CHUNK", 10000, 1, 50000)
	fail("MAX_PAGES_PER_CHUNK", err)
	s.MaxMBPerChunk, err = envInt("MAX_MB_PER_CHUNK", 50, 1, 1000)
	fail("MAX_MB_PER_CHUNK", err)
	s.XLSXMinPagesPerChunk, err = envInt("XLSX_MIN_PAGES_PER_CHUNK", 200, 1, 20000)
	fail("XLSX_MIN_PAGES_PER_CHUNK", err)
	s.PPTXMinPagesPerChunk, err = envInt("PPTX_MIN_PAGES_PER_CHUNK", 25, 1, 500)
	fail("PPTX_MIN_PAGES_PER_CHUNK", err)

	s.PoolMinChunks, err = envInt("POOL_MIN_CHUNKS", 2, 1, 64)
	fail("POOL_MIN_CHUNKS", err)
	s.XLSXMaxPoolSize, err = envInt("XLSX_MAX_POOL_SIZE", 4, 1, 16)
	fail("XLSX_MAX_POOL_SIZE", err)
	s.ForkAfterLoad = envBool("FORK_AFTER_LOAD", true)

	s.RateLimitEnabled = envBool("RATE_LIMIT_ENABLED", true)
	s.RateLimitPerIPRPM, err = envInt("RATE_LIMIT_PER_IP_RPM", 30, 1, 10000)
	fail("RATE_LIMIT_PER_IP_RPM", err)
	s.RateLimitBurst, err = envInt("RATE_LIMIT_BURST", 5, 1, 1000)
	fail("RATE_LIMIT_BURST", err)
	s.RateLimitMaxKeys, err = envInt("RATE_LIMIT_MAX_KEYS", 10000, 10, 1000000)
	fail("RATE_LIMIT_MAX_KEYS", err)
	s.RateLimitTrustXFF = envBool("RATE_LIMIT_TRUST_XFF", true)

	s.S3Enabled = envBool("S3_ENABLED", false)
	s.S3Region = envStr("S3_REGION", "")
	s.S3InputBucketsAllowlist = envStr("S3_INPUT_BUCKETS_ALLOWLIST", "")
	s.S3OutputBucketsAllowlist = envStr("S3_OUTPUT_BUCKETS_ALLOWLIST", "")
	s.S3DefaultOutputBucket = envStr("S3_DEFAULT_OUTPUT_BUCKET", "")
	s.S3OutputKeyTemplate = envStr("S3_OUTPUT_KEY_TEMPLATE", s.S3OutputKeyTemplate)
	s.S3PresignTTLSeconds, err = envInt("S3_PRESIGN_TTL_SECONDS", 900, 1, 7*24*3600)
	fail("S3_PRESIGN_TTL_SECONDS", err)
	s.S3PublicEndpoint = envStr("S3_PUBLIC_ENDPOINT", "")

	if len(errs) > 0 {
		return nil, fmt.Errorf("invalid configuration:\n  %s", strings.Join(errs, "\n  "))
	}
	return s, nil
}

// CacheEnabled reports whether a cache directory is configured.
func (s *Settings) CacheEnabled() bool { return s.CacheDir != "" }

// --- env helpers ---

func envStr(name, def string) string {
	if v, ok := os.LookupEnv(envPrefix + name); ok {
		return v
	}
	return def
}

func envInt(name string, def, min, max int) (int, error) {
	v64, err := envInt64(name, int64(def), int64(min), int64(max))
	return int(v64), err
}

func envInt64(name string, def, min, max int64) (int64, error) {
	raw, ok := os.LookupEnv(envPrefix + name)
	if !ok {
		return def, nil
	}
	v, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil {
		return def, fmt.Errorf("not an integer: %q", raw)
	}
	if v < min || v > max {
		return v, fmt.Errorf("value %d out of range [%d, %d]", v, min, max)
	}
	return v, nil
}

func envBool(name string, def bool) bool {
	raw, ok := os.LookupEnv(envPrefix + name)
	if !ok {
		return def
	}
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
}

func oneOf(v string, allowed ...string) error {
	for _, a := range allowed {
		if v == a {
			return nil
		}
	}
	return fmt.Errorf("%q not in %v", v, allowed)
}
