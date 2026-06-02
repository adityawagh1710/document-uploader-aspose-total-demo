package server

import (
	"os"
	"os/exec"

	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/license"
	"github.com/opus2/office-convert-orchestrator/internal/probe"
)

// licenseHealth is the hybrid health check: static problems computed at
// startup, license state evaluated live. Ported from HealthChecker.
type licenseHealth struct {
	settings       *config.Settings
	licenseMgr     *license.Manager
	staticProblems []string
}

// NewHealth constructs the health checker and computes static problems once.
func NewHealth(s *config.Settings, mgr *license.Manager) *licenseHealth {
	h := &licenseHealth{settings: s, licenseMgr: mgr}

	// All five per-product worker binaries must be present (docx/pptx/xlsx/pdf
	// + email), each isolating one Aspose product's CodePorting framework.
	required := append([]string{}, asString(probe.AcceptedFormats)...)
	required = append(required, "email")
	for _, wname := range required {
		if _, err := os.Stat(s.WorkerBinaryPrefix + "-" + wname); err != nil {
			h.staticProblems = append(h.staticProblems, "worker_binary_missing")
			break
		}
	}
	if _, err := exec.LookPath("qpdf"); err != nil {
		h.staticProblems = append(h.staticProblems, "qpdf_missing")
	}
	if err := os.MkdirAll(s.ScratchDir, 0o755); err != nil {
		h.staticProblems = append(h.staticProblems, "scratch_dir_unwritable")
	}
	return h
}

func (h *licenseHealth) snapshot(activeJobs int) map[string]any {
	problems := append([]string{}, h.staticProblems...)
	var daysRemaining any // nil for permanent
	days, has, err := h.licenseMgr.DaysRemaining()
	switch {
	case err != nil:
		if os.IsNotExist(err) {
			problems = append(problems, "license_path_missing")
		} else {
			problems = append(problems, "license_invalid")
		}
	case has:
		daysRemaining = days
		if days < 0 {
			problems = append(problems, "license_expired")
		}
	}
	return map[string]any{
		"ready":                  len(problems) == 0,
		"license_days_remaining": daysRemaining,
		"active_jobs":            activeJobs,
		"max_jobs":               h.settings.MaxJobs,
		"problems":               problems,
	}
}

func asString[T ~string](in []T) []string {
	out := make([]string, len(in))
	for i, v := range in {
		out[i] = string(v)
	}
	return out
}
