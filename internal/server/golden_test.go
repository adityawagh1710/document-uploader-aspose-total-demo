package server

// Golden-fixture parity gate (Phase 6 exit criterion / Phase 8 cutover gate).
//
// This replays the requests captured from the *live Python* orchestrator by
// scripts/capture_golden.py and asserts the Go port produces equivalent
// responses. It is the one test that pins Go's HTTP output against the Python
// oracle rather than against the Go code's own expectations.
//
// Why SEMANTIC comparison, not a byte diff (the spec says "byte-for-byte" but
// that's wrong in two concrete ways the capture surfaced):
//
//   - Python's json renders whole-valued floats with a decimal point (1.0)
//     where Go's encoding/json renders them bare (1). Identical to any JSON
//     parser; different bytes. We decode both sides to `any` (numbers become
//     float64) so 1 and 1.0 unify, then compare by value.
//   - The /v1/conversions next_cursor is base64(JSON{ts,id}); its embedded ts
//     inherits the same float-format split, so the cursor TOKEN bytes differ
//     while decoding to the same {ts,id}. We compare cursors by decoding.
//
// If the golden files are absent (capture not yet run in an env with Python +
// qpdf — see `make golden-capture`), the test skips with instructions.

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
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

const goldenDir = "testdata/golden"

type goldenManifest struct {
	NormDefaultHeaders []string      `json:"norm_default_headers"`
	Seed               goldenSeed    `json:"seed"`
	Cases              []goldenCase  `json:"cases"`
}

type goldenSeed struct {
	Conversions []map[string]any `json:"conversions"`
	Progress    []map[string]any `json:"progress"`
}

type goldenCase struct {
	Name            string `json:"name"`
	Method          string `json:"method"`
	Path            string `json:"path"`
	RequestID       string `json:"request_id"`
	Seed            bool   `json:"seed"`
	BodyB64         string `json:"body_b64"`
	ContentType     string `json:"content_type"`
	Repeat          int    `json:"repeat"`
	CursorField     string `json:"cursor_field"`
	NormalizeBody   []string `json:"normalize_body"`
	NormalizeHeader []string `json:"normalize_headers"`
	Config          struct {
		RateLimitEnabled bool `json:"rate_limit_enabled"`
		RateLimitPerIP   int  `json:"rate_limit_per_ip_rpm"`
		RateLimitBurst   int  `json:"rate_limit_burst"`
	} `json:"config"`
}

type goldenResponse struct {
	Status  int               `json:"status"`
	Headers map[string]string `json:"headers"`
	Body    any               `json:"body"`
}

func loadManifest(t *testing.T) (*goldenManifest, bool) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(goldenDir, "manifest.json"))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false
		}
		t.Fatalf("read manifest: %v", err)
	}
	var m goldenManifest
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse manifest: %v", err)
	}
	return &m, true
}

// buildGoldenServer mirrors buildTestServer but applies per-case rate-limit
// config and seeds the stores with the manifest's shared dataset.
func buildGoldenServer(t *testing.T, m *goldenManifest, c goldenCase) *Server {
	t.Helper()
	dir := t.TempDir()
	licPath := filepath.Join(dir, "permanent.lic")
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
		AsposeVersion:       "golden",
		S3Enabled:           false,
		S3PresignTTLSeconds: 900,
		RateLimitEnabled:    c.Config.RateLimitEnabled,
		RateLimitPerIPRPM:   c.Config.RateLimitPerIP,
		RateLimitBurst:      c.Config.RateLimitBurst,
		RateLimitMaxKeys:    1000,
	}
	cm, err := cache.NewManager("", "golden")
	if err != nil {
		t.Fatal(err)
	}
	stores := worker.Stores{
		Heartbeats: obs.HeartbeatStore(),
		Timings:    obs.TimingStore(),
		Progress:   obs.NewJobProgressStore(),
	}
	recent := obs.NewRecentStore(200)
	if c.Seed {
		seedStores(t, recent, stores, m.Seed)
	}
	h := NewHealth(s, license.NewManager(licPath))
	return New(s, h, cm, stores, recent, stubS3{}, DashboardHTML, LandingHTML)
}

func seedStores(t *testing.T, recent *obs.RecentStore, stores worker.Stores, seed goldenSeed) {
	t.Helper()
	// Record in manifest order; both impls prepend (newest-first), so identical
	// input order yields identical snapshot order.
	for _, raw := range seed.Conversions {
		b, _ := json.Marshal(raw)
		var rec obs.ConversionRecord
		if err := json.Unmarshal(b, &rec); err != nil {
			t.Fatalf("seed conversion: %v", err)
		}
		recent.Record(rec)
	}
	for _, p := range seed.Progress {
		rid, _ := p["request_id"].(string)
		phase, _ := p["phase"].(string)
		tc := int(asFloat(p["total_chunks"]))
		lp := asFloat(p["load_progress"])
		md := asFloat(p["merge_done"])
		inc := int(asFloat(p["chunks_rendered"]))
		stores.Progress.Update(rid, obs.ProgressUpdate{
			Phase: &phase, TotalChunks: &tc, LoadProgress: &lp, MergeDone: &md, IncrementChunks: inc,
		})
	}
}

func TestGoldenParity(t *testing.T) {
	m, ok := loadManifest(t)
	if !ok {
		t.Skip("no golden fixtures — run `make golden-capture` in an env with " +
			"Python + qpdf to generate internal/server/testdata/golden/")
	}
	for _, c := range m.Cases {
		t.Run(c.Name, func(t *testing.T) {
			want := loadGolden(t, c.Name)
			got := replayGo(t, m, c)
			compareGolden(t, c, m, want, got)
		})
	}
}

func loadGolden(t *testing.T, name string) goldenResponse {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(goldenDir, name+".json"))
	if err != nil {
		t.Fatalf("missing golden %s.json: %v (re-run `make golden-capture`)", name, err)
	}
	var g goldenResponse
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatalf("parse golden %s: %v", name, err)
	}
	return g
}

func replayGo(t *testing.T, m *goldenManifest, c goldenCase) goldenResponse {
	t.Helper()
	srv := buildGoldenServer(t, m, c)
	handler := srv.Handler()
	var body []byte
	if c.BodyB64 != "" {
		b, err := base64.StdEncoding.DecodeString(c.BodyB64)
		if err != nil {
			t.Fatalf("decode body_b64: %v", err)
		}
		body = b
	}
	repeat := c.Repeat
	if repeat < 1 {
		repeat = 1
	}
	var rec *httptest.ResponseRecorder
	for i := 0; i < repeat; i++ {
		var r *http.Request
		if body != nil {
			r = httptest.NewRequest(c.Method, c.Path, strings.NewReader(string(body)))
			r.Header.Set("Content-Type", c.ContentType)
		} else {
			r = httptest.NewRequest(c.Method, c.Path, nil)
		}
		r.Header.Set("X-Request-ID", c.RequestID)
		rec = httptest.NewRecorder()
		handler.ServeHTTP(rec, r)
	}
	return recorderToGolden(t, rec)
}

func recorderToGolden(t *testing.T, rec *httptest.ResponseRecorder) goldenResponse {
	t.Helper()
	resp := rec.Result()
	hdrs := map[string]string{}
	for _, h := range contractHeaders {
		if v := resp.Header.Get(h); v != "" {
			hdrs[h] = v
		}
	}
	var bodyAny any
	ct := resp.Header.Get("Content-Type")
	switch {
	case strings.HasPrefix(ct, "application/json"):
		if err := json.NewDecoder(resp.Body).Decode(&bodyAny); err != nil {
			t.Fatalf("decode go json body: %v", err)
		}
	case strings.HasPrefix(ct, "text/html"):
		b, _ := io.ReadAll(resp.Body)
		bodyAny = map[string]any{"_html_len": float64(len(b))}
	default:
		b, _ := io.ReadAll(resp.Body)
		bodyAny = map[string]any{"_text": string(b)}
	}
	return goldenResponse{Status: resp.StatusCode, Headers: hdrs, Body: bodyAny}
}

var contractHeaders = []string{
	"X-Request-ID", "Content-Type", "Content-Disposition", "Retry-After",
	"X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
}

func compareGolden(t *testing.T, c goldenCase, m *goldenManifest, want, got goldenResponse) {
	t.Helper()
	if want.Status != got.Status {
		t.Errorf("status: want %d, got %d", want.Status, got.Status)
	}
	compareHeaders(t, c, m, want.Headers, got.Headers)

	// Cursor field: decode both base64-JSON tokens and compare structurally.
	if c.CursorField != "" {
		wb, _ := want.Body.(map[string]any)
		gb, _ := got.Body.(map[string]any)
		if !cursorEqual(t, wb[c.CursorField], gb[c.CursorField]) {
			t.Errorf("cursor %q mismatch: want %v, got %v", c.CursorField, wb[c.CursorField], gb[c.CursorField])
		}
		delete(wb, c.CursorField)
		delete(gb, c.CursorField)
	}

	wantBody := normalize(deepCopy(want.Body), c.NormalizeBody)
	gotBody := normalize(deepCopy(got.Body), c.NormalizeBody)
	if diff := jsonDiff(wantBody, gotBody, ""); diff != "" {
		t.Errorf("body mismatch:\n%s\n  want=%s\n  got =%s", diff, mustJSON(wantBody), mustJSON(gotBody))
	}
}

func compareHeaders(t *testing.T, c goldenCase, m *goldenManifest, want, got map[string]string) {
	t.Helper()
	norm := map[string]bool{}
	for _, h := range append(append([]string{}, m.NormDefaultHeaders...), c.NormalizeHeader...) {
		norm[strings.ToLower(h)] = true
	}
	keys := map[string]bool{}
	for k := range want {
		keys[k] = true
	}
	for k := range got {
		keys[k] = true
	}
	for k := range keys {
		if norm[strings.ToLower(k)] {
			// Only require presence-parity for normalized headers.
			if (want[k] != "") != (got[k] != "") {
				t.Errorf("header %q presence differs: want=%q got=%q", k, want[k], got[k])
			}
			continue
		}
		wv, gv := want[k], got[k]
		if strings.EqualFold(k, "Content-Type") {
			wv, gv = mediaType(wv), mediaType(gv)
		}
		if wv != gv {
			t.Errorf("header %q: want %q, got %q", k, want[k], got[k])
		}
	}
}

// ---- helpers ----

func mediaType(v string) string {
	if i := strings.IndexByte(v, ';'); i >= 0 {
		return strings.TrimSpace(v[:i])
	}
	return strings.TrimSpace(v)
}

func cursorEqual(t *testing.T, want, got any) bool {
	t.Helper()
	if want == nil && got == nil {
		return true
	}
	ws, _ := want.(string)
	gs, _ := got.(string)
	if ws == "" || gs == "" {
		return ws == gs
	}
	return mustJSON(decodeCursor(t, ws)) == mustJSON(decodeCursor(t, gs))
}

func decodeCursor(t *testing.T, tok string) any {
	t.Helper()
	raw, err := base64.URLEncoding.DecodeString(tok)
	if err != nil {
		t.Fatalf("decode cursor %q: %v", tok, err)
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("parse cursor json %q: %v", raw, err)
	}
	return v
}

// normalize walks dotted paths ("a.b", "list[].field", "field") and replaces
// matched leaves with a sentinel so volatile values don't fail the diff.
func normalize(v any, paths []string) any {
	for _, p := range paths {
		v = normPath(v, strings.Split(p, "."))
	}
	return v
}

func normPath(v any, segs []string) any {
	if len(segs) == 0 {
		return "<NORM>"
	}
	seg := segs[0]
	if strings.HasSuffix(seg, "[]") {
		key := strings.TrimSuffix(seg, "[]")
		if m, ok := v.(map[string]any); ok {
			if lst, ok := m[key].([]any); ok {
				for i := range lst {
					lst[i] = normPath(lst[i], segs[1:])
				}
			}
			return m
		}
		// bare "[]" — v itself is the list
		if lst, ok := v.([]any); ok {
			for i := range lst {
				lst[i] = normPath(lst[i], segs[1:])
			}
		}
		return v
	}
	if m, ok := v.(map[string]any); ok {
		if _, present := m[seg]; present {
			m[seg] = normPath(m[seg], segs[1:])
		}
	}
	return v
}

func deepCopy(v any) any {
	b, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(b, &out)
	return out
}

// jsonDiff returns "" if equal, else a human path to the first divergence.
// Numbers compare by value (float64 from json), with a small tolerance.
func jsonDiff(a, b any, path string) string {
	switch av := a.(type) {
	case map[string]any:
		bv, ok := b.(map[string]any)
		if !ok {
			return fmt.Sprintf("%s: type object vs %T", path, b)
		}
		if len(av) != len(bv) {
			return fmt.Sprintf("%s: object key count %d vs %d (keys want=%v got=%v)", path, len(av), len(bv), keysOf(av), keysOf(bv))
		}
		for k, x := range av {
			y, present := bv[k]
			if !present {
				return fmt.Sprintf("%s.%s: missing in got", path, k)
			}
			if d := jsonDiff(x, y, path+"."+k); d != "" {
				return d
			}
		}
	case []any:
		bv, ok := b.([]any)
		if !ok {
			return fmt.Sprintf("%s: type array vs %T", path, b)
		}
		if len(av) != len(bv) {
			return fmt.Sprintf("%s: array len %d vs %d", path, len(av), len(bv))
		}
		for i := range av {
			if d := jsonDiff(av[i], bv[i], fmt.Sprintf("%s[%d]", path, i)); d != "" {
				return d
			}
		}
	case float64:
		bv, ok := b.(float64)
		if !ok || math.Abs(av-bv) > 1e-9 {
			return fmt.Sprintf("%s: number %v vs %v", path, a, b)
		}
	default:
		if fmt.Sprint(a) != fmt.Sprint(b) {
			return fmt.Sprintf("%s: %#v vs %#v", path, a, b)
		}
	}
	return ""
}

func keysOf(m map[string]any) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	return ks
}

func asFloat(v any) float64 {
	if f, ok := v.(float64); ok {
		return f
	}
	return 0
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
