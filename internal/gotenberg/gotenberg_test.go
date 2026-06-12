package gotenberg

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
	"github.com/opus2/office-convert-orchestrator/internal/types"
)

func writeHTML(t *testing.T) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "input.html")
	require.NoError(t, os.WriteFile(p, []byte("<!doctype html><html><body>hi</body></html>"), 0o644))
	return p
}

func failureClass(t *testing.T, err error) types.FailureClass {
	t.Helper()
	oe, ok := err.(*oerrors.Error)
	require.Truef(t, ok, "expected *oerrors.Error, got %T: %v", err, err)
	return oe.FailureClass
}

func TestConvertHTMLSuccess(t *testing.T) {
	var gotPath string
	var fields map[string][]string
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseMultipartForm(1<<20))
		fields = r.MultipartForm.Value
		f := r.MultipartForm.File["files"]
		require.Len(t, f, 1)
		gotPath = f[0].Filename
		w.Header().Set("Content-Type", "application/pdf")
		_, _ = io.WriteString(w, "%PDF-1.7 fake body")
	}))
	defer ts.Close()

	out := filepath.Join(t.TempDir(), "out.pdf")
	c := New(ts.URL, 5)
	err := c.ConvertHTML(context.Background(), writeHTML(t),
		WaitOptions{WaitDelay: "2s", WaitForExpression: "window.status === 'ready'"}, out)
	require.NoError(t, err)

	// Chromium contract: the part must be named index.html.
	assert.Equal(t, "index.html", gotPath)
	// BR-7 geometry fields + wait controls forwarded.
	assert.Equal(t, "8.5", fields["paperWidth"][0])
	assert.Equal(t, "11", fields["paperHeight"][0])
	assert.Equal(t, "0.5", fields["marginTop"][0])
	assert.Equal(t, "2s", fields["waitDelay"][0])
	assert.Equal(t, "window.status === 'ready'", fields["waitForExpression"][0])

	b, err := os.ReadFile(out)
	require.NoError(t, err)
	assert.True(t, len(b) >= 5 && string(b[:5]) == "%PDF-")
}

func TestConvertHTMLWaitFieldsOmittedWhenUnset(t *testing.T) {
	var fields map[string][]string
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseMultipartForm(1<<20))
		fields = r.MultipartForm.Value
		_, _ = io.WriteString(w, "%PDF-1.7")
	}))
	defer ts.Close()

	c := New(ts.URL, 5)
	require.NoError(t, c.ConvertHTML(context.Background(), writeHTML(t), WaitOptions{},
		filepath.Join(t.TempDir(), "out.pdf")))
	assert.NotContains(t, fields, "waitDelay")
	assert.NotContains(t, fields, "waitForExpression")
}

// BR-5 response classification table.
func TestConvertHTMLClassification(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		want   types.FailureClass
	}{
		{"4xx -> input_unprocessable", 400, "malformed page", types.InputUnprocessable},
		{"5xx -> render_failed", 503, "queue full", types.RenderFailed},
		{"non-pdf 200 -> render_failed", 200, "<html>oops</html>", types.RenderFailed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			}))
			defer ts.Close()
			c := New(ts.URL, 5)
			err := c.ConvertHTML(context.Background(), writeHTML(t), WaitOptions{},
				filepath.Join(t.TempDir(), "out.pdf"))
			require.Error(t, err)
			assert.Equal(t, tc.want, failureClass(t, err))
		})
	}
}

func TestConvertHTMLEngineUnreachable(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := ts.URL
	ts.Close() // connection refused from here on

	c := New(url, 2)
	err := c.ConvertHTML(context.Background(), writeHTML(t), WaitOptions{},
		filepath.Join(t.TempDir(), "out.pdf"))
	require.Error(t, err)
	assert.Equal(t, types.EngineUnavailable, failureClass(t, err))
}

func TestConvertHTMLEngineNotConfigured(t *testing.T) {
	c := New("", 2)
	err := c.ConvertHTML(context.Background(), writeHTML(t), WaitOptions{},
		filepath.Join(t.TempDir(), "out.pdf"))
	require.Error(t, err)
	assert.Equal(t, types.EngineUnavailable, failureClass(t, err))
}
