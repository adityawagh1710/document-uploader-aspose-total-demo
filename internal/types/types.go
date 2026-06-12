// Package types holds the shared domain types used across the orchestrator.
//
// Ported from office_convert/types.py. The Python originals are frozen
// dataclasses; the Go equivalents are value types passed by value (or as
// immutable-by-convention structs) so they are safe to share across
// goroutines without mutation mid-flight.
package types

// FormatName is the set of formats that flow through the Aspose chunk planner.
type FormatName string

const (
	FormatDOCX FormatName = "docx"
	FormatPPTX FormatName = "pptx"
	FormatXLSX FormatName = "xlsx"
	FormatPDF  FormatName = "pdf"
)

// AsposeFormats is the closed set of planner-eligible formats.
var AsposeFormats = []FormatName{FormatDOCX, FormatPPTX, FormatXLSX, FormatPDF}

// DispatchFormat is the wider set returned by probe.DetectFormat: it includes
// formats that do NOT go through the Aspose chunk planner.
//
//   - ODG + raster/vector image formats -> LibreOffice fallback (no native
//     Aspose product for drawing-page geometry or image-to-PDF in C++).
//   - EML -> aspose_email_convert pipeline (Aspose.Email -> MHTML, then
//     worker-docx renders MHTML -> PDF; the chain runs out of band because
//     Aspose.Email's cs2cpp framework must stay process-isolated from
//     Aspose.Words's).
//
// The orchestrator + workers still operate on FormatName; the server routes
// DispatchFormat \ FormatName to the libreoffice or email path before the
// orchestrator is even constructed.
type DispatchFormat string

const (
	DispatchDOCX DispatchFormat = "docx"
	DispatchPPTX DispatchFormat = "pptx"
	DispatchXLSX DispatchFormat = "xlsx"
	DispatchPDF  DispatchFormat = "pdf"
	DispatchODG  DispatchFormat = "odg"
	DispatchPNG  DispatchFormat = "png"
	DispatchJPG  DispatchFormat = "jpg"
	DispatchTIFF DispatchFormat = "tiff"
	DispatchGIF  DispatchFormat = "gif"
	DispatchBMP  DispatchFormat = "bmp"
	DispatchWEBP DispatchFormat = "webp"
	DispatchSVG  DispatchFormat = "svg"
	DispatchEML  DispatchFormat = "eml"
	// DispatchHTML routes to the engine-specific /v1/convert/html/{engine}
	// endpoints (Gotenberg or worker-docx single-shot); never the chunk planner.
	DispatchHTML DispatchFormat = "html"
)

// FailureClass enumerates the canonical failure classes returned to callers in
// the Diagnostic body. String values MUST match types.py exactly — they are a
// wire contract.
type FailureClass string

const (
	UnsupportedFormat        FailureClass = "unsupported_format"
	MissingFile              FailureClass = "missing_file"
	InputTooLarge            FailureClass = "input_too_large"
	InputUnprocessable       FailureClass = "input_unprocessable"
	RenderFailed             FailureClass = "render_failed"
	SubdivisionFloorExceeded FailureClass = "subdivision_floor_exceeded"
	MergeFailed              FailureClass = "merge_failed"
	LicenseExpired           FailureClass = "license_expired"
	Busy                     FailureClass = "busy"
	RateLimited              FailureClass = "rate_limited"
	// S3 source/sink integration.
	InputSourceConflict  FailureClass = "input_source_conflict"
	S3Disabled           FailureClass = "s3_disabled"
	S3InvalidURL         FailureClass = "s3_invalid_url"
	S3InputNotFound      FailureClass = "s3_input_not_found"
	S3InputForbidden     FailureClass = "s3_input_forbidden"
	S3OutputForbidden    FailureClass = "s3_output_forbidden"
	S3OutputUploadFailed FailureClass = "s3_output_upload_failed"
	// EngineUnavailable: the Gotenberg conversion engine is down, unreachable,
	// or not configured. Go-only extension of the taxonomy (HTML feature);
	// recorded as a deliberate parity divergence until Python retirement.
	EngineUnavailable FailureClass = "engine_unavailable"
)

// LicenseState is computed from days_remaining per business-rules.md §4.
type LicenseState string

const (
	LicenseStatePermanent     LicenseState = "permanent"
	LicenseStateHealthy       LicenseState = "healthy"
	LicenseStateWarn          LicenseState = "warn"
	LicenseStateCritical      LicenseState = "critical"
	LicenseStateExpiringToday LicenseState = "expiring_today"
	LicenseStateExpired       LicenseState = "expired"
)

// Chunk is a planning unit. One Chunk = one Aspose subprocess invocation.
//
// Index is a float because subdivision produces fractional sub-chunk indices.
// PageRange is inclusive and 1-based: [start, end].
type Chunk struct {
	Index       float64
	PageStart   int // inclusive, 1-based
	PageEnd     int // inclusive, 1-based
	NaturalSeam bool
}

// Pages returns the inclusive page count covered by the chunk.
func (c Chunk) Pages() int {
	return c.PageEnd - c.PageStart + 1
}

// ChunkPlan is an ordered, complete, non-overlapping cover of [1..TotalPages].
type ChunkPlan struct {
	Chunks      []Chunk
	TotalPages  int
	EstimatedMB float64
}

// ProbeResult is the output of the probe step, consumed by planner.PlanChunks.
type ProbeResult struct {
	PageCount    int
	Format       FormatName
	NaturalSeams [][2]int
	SizeBytes    int64
}

// ConversionOptions holds per-request caller-supplied options from the
// multipart options JSON. Cache defaults to true (see config/parse).
type ConversionOptions struct {
	Cache    bool
	LogLevel string // empty == unset
}

// ConversionResult is metadata about a successful conversion, surfaced via
// X-* response headers.
type ConversionResult struct {
	ChunksRendered     int
	SubdivisionRetries int
	CacheHits          int
	DurationSeconds    float64
}

// Diagnostic is structured failure metadata, returned as the HTTP error body.
type Diagnostic struct {
	RequestID    string         `json:"request_id"`
	FailureClass FailureClass   `json:"failure_class"`
	Detail       map[string]any `json:"detail"`
}
