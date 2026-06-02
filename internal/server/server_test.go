package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
	if err := os.WriteFile(licPath, []byte(`<License><Data><Product>Aspose.Total for C++</Product></Data></License>`), 0o644); err != nil {
		t.Fatal(err)
	}
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
	if err != nil {
		t.Fatal(err)
	}
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
		if _, ok := body[k]; !ok {
			t.Errorf("health missing key %q (body=%v)", k, body)
		}
	}
	if resp.Header.Get("X-Request-ID") == "" {
		t.Error("missing X-Request-ID echo")
	}
}

func TestDashboardAndLandingServed(t *testing.T) {
	srv := buildTestServer(t)
	for _, path := range []string{"/", "/v1/dashboard"} {
		resp := do(t, srv, "GET", path, "")
		if resp.StatusCode != 200 {
			t.Errorf("%s = %d, want 200", path, resp.StatusCode)
		}
		if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/html") {
			t.Errorf("%s content-type = %q", path, ct)
		}
	}
}

func TestConversionsEmptyShape(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/v1/conversions", "")
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	if body["has_more"] != false {
		t.Errorf("empty has_more = %v, want false", body["has_more"])
	}
	if _, ok := body["entries"]; !ok {
		t.Error("missing entries key")
	}
}

func TestPresignDisabled(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/v1/downloads/presign?bucket=b&key=k", "")
	if resp.StatusCode != 400 {
		t.Fatalf("presign disabled = %d, want 400", resp.StatusCode)
	}
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	if body["failure_class"] != "s3_disabled" {
		t.Errorf("failure_class = %v, want s3_disabled", body["failure_class"])
	}
}

func TestConvertMissingFile(t *testing.T) {
	srv := buildTestServer(t)
	// POST with an empty multipart body: no file, no s3_input -> missing_file.
	r := httptest.NewRequest("POST", "/v1/convert", strings.NewReader(""))
	r.Header.Set("Content-Type", "multipart/form-data; boundary=xyz")
	w := httptest.NewRecorder()
	srv.Handler().ServeHTTP(w, r)
	resp := w.Result()
	if resp.StatusCode != 400 {
		t.Fatalf("missing file = %d, want 400", resp.StatusCode)
	}
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	if body["failure_class"] != "missing_file" {
		t.Errorf("failure_class = %v, want missing_file", body["failure_class"])
	}
}

func TestProgressUnknown(t *testing.T) {
	srv := buildTestServer(t)
	resp := do(t, srv, "GET", "/v1/jobs/nonexistent/progress", "")
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	if body["phase"] != "unknown" {
		t.Errorf("unknown job phase = %v, want unknown", body["phase"])
	}
}
