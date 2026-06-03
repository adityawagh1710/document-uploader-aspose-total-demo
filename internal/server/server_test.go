package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/opus2/office-convert-orchestrator/internal/cache"
	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/license"
	"github.com/opus2/office-convert-orchestrator/internal/obs"
	"github.com/opus2/office-convert-orchestrator/internal/worker"
)

type stubS3 struct{}

func (stubS3) DownloadToPath(url, dest string, s *config.Settings) (string, error) { return "", nil }
func (stubS3) UploadFile(localPath, bucket, key string, s *config.Settings) error  { return nil }
func (stubS3) PresignGetURL(bucket, key string, s *config.Settings) (string, error) {
	return "https://example/signed", nil
}

func buildTestServer(t *testing.T) *Server {
	t.Helper()
	dir := t.TempDir()
	licPath := filepath.Join(dir, "permanent.lic")
	// Permanent license: well-formed, no SubscriptionExpiry -> never expired.
	require.NoError(t, os.WriteFile(licPath, []byte(`<License><Data><Product>Aspose.Total for C++</Product></Data></License>`), 0o644))
	s := &config.Settings{
		MaxJobs:             2,
		Parallel:            2,
		ScratchDir:          dir,
		LicensePath:         licPath,
		WorkerBinaryPrefix:  filepath.Join(dir, "worker"),
		MaxInputBytes:       1 << 30,
		ChunkTimeoutSeconds: 300,
		WorkerRAMBytes:      6 << 30,
		AsposeVersion:       "test",
		RateLimitEnabled:    false,
		S3Enabled:           false,
		S3PresignTTLSeconds: 900,
	}
	c, err := cache.NewManager("", "test")
	require.NoError(t, err)
	stores := worker.Stores{
		Heartbeats: obs.HeartbeatStore(),
		Timings:    obs.TimingStore(),
		Progress:   obs.NewJobProgressStore(),
	}
	h := NewHealth(s, license.NewManager(licPath))
	return New(s, h, c, stores, obs.NewRecentStore(200), stubS3{}, DashboardHTML, LandingHTML)
}

func do(t *testing.T, srv *Server, method, target string, body string) *http.Response {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, target, strings.NewReader(body))
	} else {
		r = httptest.NewRequest(method, target, nil)
	}
	w := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w, r)
	return w.Result()
}

func TestHealthShape(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/health", "")
	// Workers missing -> not ready -> 503, but the JSON shape must be intact.
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	for _, k := range []string{"ready", "license_days_remaining", "active_jobs", "max_jobs", "problems"} {
		assert.Containsf(t, body, k, "health missing key %q", k)
	}
	assert.NotEmpty(t, resp.Header.Get("X-Request-ID"), "missing X-Request-ID echo")
}

func TestDashboardAndLandingServed(t *testing.T) {
	srv := buildTestServer(t)
	for _, path := range []string{"/", "/v1/dashboard"} {
		resp := do(t, srv, "GET", path, "")
		assert.Equalf(t, 200, resp.StatusCode, "%s status", path)
		assert.Truef(t, strings.HasPrefix(resp.Header.Get("Content-Type"), "text/html"),
			"%s content-type = %q", path, resp.Header.Get("Content-Type"))
	}
}

func TestConversionsEmptyShape(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/v1/conversions", "")
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	assert.Equal(t, false, body["has_more"], "empty has_more")
	assert.Contains(t, body, "entries")
}

func TestPresignDisabled(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/v1/downloads/presign?bucket=b&key=k", "")
	require.Equal(t, 400, resp.StatusCode, "presign disabled")
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	assert.Equal(t, "s3_disabled", body["failure_class"])
}

func TestConvertMissingFile(t *testing.T) {
	srv := buildTestServer(t)
	// POST with an empty multipart body: no file, no s3_input -> missing_file.
	r := httptest.NewRequest("POST", "/v1/convert", strings.NewReader(""))
	r.Header.Set("Content-Type", "multipart/form-data; boundary=xyz")
	w := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w, r)
	resp := w.Result()
	require.Equal(t, 400, resp.StatusCode, "missing file")
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	assert.Equal(t, "missing_file", body["failure_class"])
}

func TestProgressUnknown(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/v1/jobs/nonexistent/progress", "")
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	assert.Equal(t, "unknown", body["phase"], "unknown job phase")
}
