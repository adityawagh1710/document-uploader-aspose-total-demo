// Package libreoffice is the LibreOffice fallback for formats Aspose.Total C++
// cannot render (ODG + raster/vector images).
//
// Ported from office_convert/libreoffice_convert.py. Per-request invocation of
// `soffice --headless --convert-to pdf` with a per-call profile dir.
package libreoffice

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
)

const sofficeBin = "soffice"

// ConvertToPDF converts inputPath to PDF via headless LibreOffice and returns
// the produced PDF path (under outputDir). Mirrors convert_to_pdf.
func ConvertToPDF(ctx context.Context, inputPath, outputDir string, timeoutSeconds int) (string, error) {
	if _, err := exec.LookPath(sofficeBin); err != nil {
		return "", &oerrors.Error{FailureClass: "render_failed", HTTPStatus: 500, Msg: "libreoffice (soffice) not installed"}
	}
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return "", oerrors.NewRender(nil, -1, "mkdir outdir: "+err.Error())
	}
	profileDir := filepath.Join(outputDir, "lo_profile")
	if err := os.MkdirAll(profileDir, 0o755); err != nil {
		return "", oerrors.NewRender(nil, -1, "mkdir profile: "+err.Error())
	}

	cmd := exec.Command(sofficeBin,
		"-env:UserInstallation=file://"+profileDir,
		"--headless", "--nologo", "--nofirststartwizard", "--norestore",
		"--convert-to", "pdf", "--outdir", outputDir, inputPath,
	)
	stdout, stderr, rc, timedOut := runWithTimeout(cmd, time.Duration(timeoutSeconds)*time.Second)
	if timedOut {
		return "", oerrors.NewRender(nil, -1, "libreoffice timed out")
	}
	if rc != 0 {
		tail := stderr
		if tail == "" {
			tail = stdout
		}
		tail = tailStr(tail, 1024)
		return "", oerrors.NewRender(nil, nonZero(rc), "libreoffice: "+strings.TrimSpace(tail))
	}

	// soffice writes <stem>.pdf into --outdir.
	stem := strings.TrimSuffix(filepath.Base(inputPath), filepath.Ext(inputPath))
	expected := filepath.Join(outputDir, stem+".pdf")
	if _, err := os.Stat(expected); err == nil {
		return expected, nil
	}
	// Some builds normalize the stem; scan for any .pdf.
	matches, _ := filepath.Glob(filepath.Join(outputDir, "*.pdf"))
	if len(matches) == 0 {
		return "", oerrors.NewRender(nil, 0, "libreoffice exited 0 but no .pdf appeared in outdir")
	}
	sort.Strings(matches)
	return matches[0], nil
}

func runWithTimeout(cmd *exec.Cmd, timeout time.Duration) (stdout, stderr string, rc int, timedOut bool) {
	var outBuf, errBuf strings.Builder
	cmd.Stdout = &outBuf
	cmd.Stderr = &errBuf
	if err := cmd.Start(); err != nil {
		return "", err.Error(), 1, false
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-time.After(timeout):
		_ = cmd.Process.Kill()
		<-done
		return outBuf.String(), errBuf.String(), -1, true
	case <-done:
		rc = 0
		if cmd.ProcessState != nil {
			rc = cmd.ProcessState.ExitCode()
		}
		return outBuf.String(), errBuf.String(), rc, false
	}
}

func nonZero(rc int) int {
	if rc == 0 {
		return 1
	}
	return rc
}

func tailStr(s string, n int) string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}
