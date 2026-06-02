// Package email is the Aspose.Email EML -> PDF pipeline.
//
// Ported from office_convert/aspose_email_convert.py. Two-stage pipeline:
//
//  1. worker-email  loads EML, saves MHTML.
//  2. worker-docx   probes the MHTML page count, then renders MHTML -> PDF.
//
// The two workers run in distinct processes so Aspose.Email's CodePorting
// framework never coexists with Aspose.Words's in one address space. The
// subprocess mechanics reuse worker.RunWorker (same prlimit + exit-code map).
package email

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
	"github.com/opus2/office-convert-orchestrator/internal/types"
	"github.com/opus2/office-convert-orchestrator/internal/worker"
)

// ConvertToPDF converts an EML at inputPath to a PDF under scratchDir and
// returns the produced PDF path. Mirrors aspose_email_convert.convert_to_pdf.
func ConvertToPDF(ctx context.Context, s *config.Settings, inputPath, scratchDir, requestID string) (string, error) {
	if err := os.MkdirAll(scratchDir, 0o755); err != nil {
		return "", oerrors.NewRender(nil, -1, "mkdir email scratch: "+err.Error())
	}
	stem := strings.TrimSuffix(filepath.Base(inputPath), filepath.Ext(inputPath))
	mhtPath := filepath.Join(scratchDir, stem+".mht")
	pdfPath := filepath.Join(scratchDir, stem+".pdf")

	// Stage 1: EML -> MHT via worker-email.
	r1 := [2]int{1, 1} // emails don't paginate; placeholder satisfies the CLI.
	if _, _, err := runWorker(ctx, s, "render", inputPath, "email", mhtPath, &r1, requestID); err != nil {
		return "", err
	}
	if _, err := os.Stat(mhtPath); err != nil {
		return "", oerrors.NewRender(nil, 0, "worker-email exited 0 but produced no MHT output")
	}

	// Stage 2: probe MHT via worker-docx for the rendered page count.
	probeOut, _, err := runWorker(ctx, s, "probe", mhtPath, "docx", "", nil, requestID)
	if err != nil {
		return "", err
	}
	var probe struct {
		PageCount *int `json:"page_count"`
	}
	if err := json.Unmarshal(probeOut, &probe); err != nil || probe.PageCount == nil {
		return "", oerrors.NewRender(nil, 0, "worker-docx probe returned malformed JSON")
	}
	if *probe.PageCount < 1 {
		return "", oerrors.NewRender(nil, 0, "worker-docx probe reported page_count<1")
	}

	// Stage 3: MHT -> PDF via worker-docx across the full page range.
	r3 := [2]int{1, *probe.PageCount}
	if _, _, err := runWorker(ctx, s, "render", mhtPath, "docx", pdfPath, &r3, requestID); err != nil {
		return "", err
	}
	if _, err := os.Stat(pdfPath); err != nil {
		return "", oerrors.NewRender(nil, 0, "worker-docx exited 0 but produced no PDF output")
	}
	return pdfPath, nil
}

// runWorker calls the worker package's one-shot runner. format is "email" or
// "docx"; chunk is always nil (emails bypass the chunk planner).
func runWorker(ctx context.Context, s *config.Settings, mode, inputPath string, format types.FormatName, outputPath string, pageRange *[2]int, requestID string) ([]byte, []byte, error) {
	return worker.RunWorker(ctx, s, mode, inputPath, format, outputPath, pageRange, requestID, nil)
}
