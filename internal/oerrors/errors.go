// Package oerrors is the internal conversion-error hierarchy.
//
// Ported from office_convert/errors.py. Implements FR-5. Each error carries a
// FailureClass and HTTPStatus so the HTTP handler can map it to the canonical
// response, plus a Detail map mirroring as_detail_dict().
//
// Named oerrors (not errors) to avoid shadowing the stdlib errors package.
package oerrors

import (
	"fmt"

	"github.com/opus2/office-convert-orchestrator/internal/types"
)

// Error is the base conversion error. All caller-facing failures are an *Error.
// OOM marks the internal subdivision-trigger variant that never surfaces over
// HTTP. ExitCode/Chunk are populated for render failures.
type Error struct {
	FailureClass types.FailureClass
	HTTPStatus   int
	Msg          string
	Detail       map[string]any

	// OOM is true for the subdivision-trigger render variant (exit 137).
	OOM bool
	// Chunk is the offending chunk for render/subdivision errors (nil otherwise).
	Chunk *types.Chunk
}

func (e *Error) Error() string { return e.Msg }

// DetailDict returns the detail body. Mirrors as_detail_dict(): for the base
// case it is {"message": msg-or-failure-class}.
func (e *Error) DetailDict() map[string]any {
	if e.Detail != nil {
		return e.Detail
	}
	msg := e.Msg
	if msg == "" {
		msg = string(e.FailureClass)
	}
	return map[string]any{"message": msg}
}

// --- constructors (one per errors.py subclass) ---

// NewUnsupportedFormat mirrors UnsupportedFormatError. reason may be "".
func NewUnsupportedFormat(detectedMagic string, accepted []string, reason string) *Error {
	msg := fmt.Sprintf("unsupported format (magic=%s)", detectedMagic)
	if reason != "" {
		msg = fmt.Sprintf("%s: %s", msg, reason)
	}
	detail := map[string]any{"detected_magic": detectedMagic, "accepted": accepted}
	if reason != "" {
		detail["reason"] = reason
	}
	return &Error{FailureClass: types.UnsupportedFormat, HTTPStatus: 400, Msg: msg, Detail: detail}
}

// NewMissingFile mirrors MissingFileError.
func NewMissingFile(msg string) *Error {
	return &Error{FailureClass: types.MissingFile, HTTPStatus: 400, Msg: msg}
}

// NewInputTooLarge mirrors InputTooLargeError.
func NewInputTooLarge(sizeBytes, ceilingBytes int64) *Error {
	return &Error{
		FailureClass: types.InputTooLarge,
		HTTPStatus:   400,
		Msg:          fmt.Sprintf("input too large: %d > %d", sizeBytes, ceilingBytes),
		Detail:       map[string]any{"size_bytes": sizeBytes, "ceiling_bytes": ceilingBytes},
	}
}

// NewInputUnprocessable mirrors InputUnprocessableError.
func NewInputUnprocessable(reason string) *Error {
	return &Error{
		FailureClass: types.InputUnprocessable,
		HTTPStatus:   422,
		Msg:          reason,
		Detail:       map[string]any{"reason": reason},
	}
}

// NewRender mirrors RenderError. chunk may be nil.
func NewRender(chunk *types.Chunk, exitCode int, stderrTail string) *Error {
	tail := stderrTail
	if len(tail) > 200 {
		tail = tail[:200]
	}
	return &Error{
		FailureClass: types.RenderFailed,
		HTTPStatus:   500,
		Msg:          fmt.Sprintf("render failed (exit=%d): %s", exitCode, tail),
		Chunk:        chunk,
		Detail:       renderDetail(chunk, exitCode, stderrTail),
	}
}

// NewOOM mirrors OOMError — internal, exit 137, caught by the orchestrator and
// translated to subdivision. Never surfaces as an HTTP response.
func NewOOM(chunk types.Chunk) *Error {
	e := NewRender(&chunk, 137, "OOM (exit 137)")
	e.OOM = true
	return e
}

func renderDetail(chunk *types.Chunk, exitCode int, stderrTail string) map[string]any {
	tail := stderrTail
	if len(tail) > 1024 {
		tail = tail[len(tail)-1024:]
	}
	d := map[string]any{"exit_code": exitCode, "stderr_tail": tail}
	if chunk != nil {
		d["chunk_index"] = chunk.Index
		d["page_range"] = []int{chunk.PageStart, chunk.PageEnd}
	} else {
		d["chunk_index"] = nil
		d["page_range"] = nil
	}
	return d
}

// NewSubdivisionFloor mirrors SubdivisionFloorError.
func NewSubdivisionFloor(chunk types.Chunk, attempts int) *Error {
	return &Error{
		FailureClass: types.SubdivisionFloorExceeded,
		HTTPStatus:   500,
		Msg:          fmt.Sprintf("subdivision floor reached on chunk %v", chunk.Index),
		Chunk:        &chunk,
		Detail: map[string]any{
			"failing_page_range": []int{chunk.PageStart, chunk.PageEnd},
			"attempts":           attempts,
		},
	}
}

// NewMerge mirrors MergeError.
func NewMerge(exitCode int, stderrTail string) *Error {
	tail := stderrTail
	if len(tail) > 1024 {
		tail = tail[len(tail)-1024:]
	}
	return &Error{
		FailureClass: types.MergeFailed,
		HTTPStatus:   500,
		Msg:          fmt.Sprintf("qpdf merge failed (exit=%d)", exitCode),
		Detail:       map[string]any{"exit_code": exitCode, "stderr_tail": tail},
	}
}

// NewLicenseExpired mirrors LicenseExpiredError. expiredOn may be "".
func NewLicenseExpired(expiredOn string) *Error {
	msg := "license expired"
	if expiredOn != "" {
		msg = "license expired on " + expiredOn
	}
	var detailOn any
	if expiredOn != "" {
		detailOn = expiredOn
	}
	return &Error{
		FailureClass: types.LicenseExpired,
		HTTPStatus:   503,
		Msg:          msg,
		Detail:       map[string]any{"expired_on": detailOn},
	}
}

// NewBusy mirrors BusyError.
func NewBusy(retryAfterSeconds int) *Error {
	return &Error{
		FailureClass: types.Busy,
		HTTPStatus:   503,
		Msg:          "server at max_jobs capacity",
		Detail:       map[string]any{"retry_after_seconds": retryAfterSeconds},
	}
}

// NewRateLimited mirrors RateLimitedError.
func NewRateLimited(retryAfterSeconds, limit int) *Error {
	return &Error{
		FailureClass: types.RateLimited,
		HTTPStatus:   429,
		Msg:          fmt.Sprintf("rate limit exceeded (%d req/min/IP)", limit),
		Detail:       map[string]any{"retry_after_seconds": retryAfterSeconds, "limit": limit},
	}
}

// --- S3 source/sink integration ---

// NewInputSourceConflict mirrors InputSourceConflictError.
func NewInputSourceConflict() *Error {
	return &Error{
		FailureClass: types.InputSourceConflict,
		HTTPStatus:   400,
		Msg:          "provide exactly one input source: 'file' or 's3_input'",
	}
}

// NewS3Disabled mirrors S3DisabledError.
func NewS3Disabled() *Error {
	return &Error{
		FailureClass: types.S3Disabled,
		HTTPStatus:   400,
		Msg:          "S3 integration is disabled (set OFFICE_CONVERT_S3_ENABLED=1)",
	}
}

// NewS3InvalidURL mirrors S3InvalidUrlError.
func NewS3InvalidURL(url string) *Error {
	return &Error{
		FailureClass: types.S3InvalidURL,
		HTTPStatus:   400,
		Msg:          fmt.Sprintf("invalid s3 url %q (expected s3://bucket/key)", url),
		Detail:       map[string]any{"url": url},
	}
}

// NewS3InputNotFound mirrors S3InputNotFoundError.
func NewS3InputNotFound(bucket, key string) *Error {
	return &Error{
		FailureClass: types.S3InputNotFound,
		HTTPStatus:   404,
		Msg:          fmt.Sprintf("s3 input not found: s3://%s/%s", bucket, key),
		Detail:       map[string]any{"bucket": bucket, "key": key},
	}
}

// NewS3InputForbidden mirrors S3InputForbiddenError.
func NewS3InputForbidden(bucket string) *Error {
	return &Error{
		FailureClass: types.S3InputForbidden,
		HTTPStatus:   400,
		Msg:          "s3 input bucket not in allowlist: " + bucket,
		Detail:       map[string]any{"bucket": bucket},
	}
}

// NewS3OutputForbidden mirrors S3OutputForbiddenError.
func NewS3OutputForbidden(bucket string) *Error {
	return &Error{
		FailureClass: types.S3OutputForbidden,
		HTTPStatus:   400,
		Msg:          "s3 output bucket not in allowlist: " + bucket,
		Detail:       map[string]any{"bucket": bucket},
	}
}

// NewS3OutputUploadFailed mirrors S3OutputUploadFailedError.
func NewS3OutputUploadFailed(bucket, key, reason string) *Error {
	return &Error{
		FailureClass: types.S3OutputUploadFailed,
		HTTPStatus:   500,
		Msg:          fmt.Sprintf("s3 output upload failed for s3://%s/%s: %s", bucket, key, reason),
		Detail:       map[string]any{"bucket": bucket, "key": key, "reason": reason},
	}
}
