// Package gotenberg is the HTTP client for the Gotenberg (Chromium) HTML→PDF
// engine — the /v1/convert/html/gotenberg endpoint's render backend.
//
// Contract (functional-design business-logic-model.md, Flow A):
//   - POST {base}/forms/chromium/convert/html, multipart; the HTML part MUST
//     be named index.html (Chromium contract).
//   - BR-7 fair-comparison geometry: US Letter + 0.5in margins on every call.
//   - Optional JS wait controls (waitDelay, waitForExpression) forwarded
//     verbatim; validation happens in the HTTP handler (BR-3).
//   - Response classification (BR-5): transport error → engine_unavailable
//     (503); 4xx → input_unprocessable (422); 5xx → render_failed (500);
//     output must start with %PDF- (BR-8).
package gotenberg

import (
	"context"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
)

const engineName = "gotenberg"

// WaitOptions are the caller-supplied JS wait controls (BR-3, already
// validated by the handler).
type WaitOptions struct {
	WaitDelay         string // e.g. "2s"; "" == unset
	WaitForExpression string // e.g. "window.status === 'ready'"; "" == unset
}

// Client converts HTML files via a Gotenberg service.
type Client struct {
	baseURL string
	http    *http.Client
}

// New builds a client. baseURL == "" means the engine is not configured;
// ConvertHTML then fails fast with engine_unavailable.
func New(baseURL string, timeoutSeconds int) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http:    &http.Client{Timeout: time.Duration(timeoutSeconds) * time.Second},
	}
}

// ConvertHTML renders htmlPath to a PDF at outPath. Returns a typed
// *oerrors.Error on every failure path (BR-5 classification).
func (c *Client) ConvertHTML(ctx context.Context, htmlPath string, opts WaitOptions, outPath string) error {
	if c.baseURL == "" {
		return oerrors.NewEngineUnavailable(engineName, "", "engine not configured (OFFICE_CONVERT_GOTENBERG_URL is empty)")
	}
	endpoint := c.baseURL + "/forms/chromium/convert/html"

	body, contentType, err := buildForm(htmlPath, opts)
	if err != nil {
		return oerrors.NewRender(nil, -1, "gotenberg form: "+err.Error())
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, body)
	if err != nil {
		return oerrors.NewRender(nil, -1, "gotenberg request: "+err.Error())
	}
	req.Header.Set("Content-Type", contentType)

	resp, err := c.http.Do(req)
	if err != nil {
		// No HTTP response at all: connect refused/reset, DNS failure, or
		// client timeout — the engine is unreachable from our point of view.
		return oerrors.NewEngineUnavailable(engineName, c.baseURL, err.Error())
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return writePDF(resp.Body, outPath)
	case resp.StatusCode >= 400 && resp.StatusCode < 500:
		return oerrors.NewInputUnprocessable(
			fmt.Sprintf("gotenberg rejected the page (HTTP %d): %s", resp.StatusCode, bodyTail(resp.Body)))
	default:
		return oerrors.NewRender(nil, -1,
			fmt.Sprintf("gotenberg HTTP %d: %s", resp.StatusCode, bodyTail(resp.Body)))
	}
}

// buildForm assembles the multipart body in memory. HTML inputs are capped at
// HTMLMaxBytes (10 MiB default) by the handler, so buffering is bounded.
func buildForm(htmlPath string, opts WaitOptions) (io.Reader, string, error) {
	var buf strings.Builder
	w := multipart.NewWriter(&buf)

	part, err := w.CreateFormFile("files", "index.html")
	if err != nil {
		return nil, "", err
	}
	f, err := os.Open(htmlPath)
	if err != nil {
		return nil, "", err
	}
	defer f.Close()
	if _, err := io.Copy(part, f); err != nil {
		return nil, "", err
	}

	// BR-7: identical page geometry on both engines (inches).
	fields := map[string]string{
		"paperWidth": "8.5", "paperHeight": "11",
		"marginTop": "0.5", "marginBottom": "0.5",
		"marginLeft": "0.5", "marginRight": "0.5",
	}
	if opts.WaitDelay != "" {
		fields["waitDelay"] = opts.WaitDelay
	}
	if opts.WaitForExpression != "" {
		fields["waitForExpression"] = opts.WaitForExpression
	}
	for k, v := range fields {
		if err := w.WriteField(k, v); err != nil {
			return nil, "", err
		}
	}
	if err := w.Close(); err != nil {
		return nil, "", err
	}
	return strings.NewReader(buf.String()), w.FormDataContentType(), nil
}

// writePDF streams the response to outPath, enforcing the BR-8 %PDF- magic on
// the first bytes before anything is considered a success.
func writePDF(body io.Reader, outPath string) error {
	out, err := os.Create(outPath)
	if err != nil {
		return oerrors.NewRender(nil, -1, "create output: "+err.Error())
	}
	defer out.Close()

	head := make([]byte, 5)
	n, err := io.ReadFull(body, head)
	if err != nil || string(head[:n]) != "%PDF-" {
		return oerrors.NewRender(nil, -1, "gotenberg returned non-PDF output")
	}
	if _, err := out.Write(head); err != nil {
		return oerrors.NewRender(nil, -1, "write output: "+err.Error())
	}
	if _, err := io.Copy(out, body); err != nil {
		return oerrors.NewRender(nil, -1, "stream output: "+err.Error())
	}
	return nil
}

func bodyTail(r io.Reader) string {
	b, _ := io.ReadAll(io.LimitReader(r, 512))
	return strings.TrimSpace(string(b))
}
