// Package s3 holds the optional S3 source/sink helpers.
//
// Ported from office_convert/s3_client.py. The pure URL/allowlist/target
// helpers have no network dependency and are always compiled. The actual
// download/upload/presign operations live behind the Ops interface (aws.go),
// so the server depends only on the interface — S3 is disabled by default
// (OFFICE_CONVERT_S3_ENABLED=0), in which case Ops is never invoked.
package s3

import (
	"strings"

	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
)

const scheme = "s3://"

// ParseURL splits s3://bucket/key into (bucket, key). key may be empty (a
// bucket-only URL). Returns an S3InvalidURL error on a malformed scheme or
// missing bucket. Mirrors parse_s3_url.
func ParseURL(url string) (bucket, key string, err error) {
	if url == "" || !strings.HasPrefix(url, scheme) {
		return "", "", oerrors.NewS3InvalidURL(url)
	}
	rest := url[len(scheme):]
	bucket, key, _ = strings.Cut(rest, "/")
	if bucket == "" {
		return "", "", oerrors.NewS3InvalidURL(url)
	}
	return bucket, key, nil
}

// ParseAllowlist parses a comma-separated bucket allowlist. Mirrors parse_allowlist.
func ParseAllowlist(raw string) []string {
	if raw == "" {
		return nil
	}
	var out []string
	for _, b := range strings.Split(raw, ",") {
		if t := strings.TrimSpace(b); t != "" {
			out = append(out, t)
		}
	}
	return out
}

// IsInputBucketAllowed fails closed: an empty allowlist rejects every bucket.
func IsInputBucketAllowed(bucket string, s *config.Settings) bool {
	for _, b := range ParseAllowlist(s.S3InputBucketsAllowlist) {
		if b == bucket {
			return true
		}
	}
	return false
}

// IsOutputBucketAllowed fails closed, but the configured default output bucket
// is implicitly allowed. Mirrors is_output_bucket_allowed.
func IsOutputBucketAllowed(bucket string, s *config.Settings) bool {
	for _, b := range ParseAllowlist(s.S3OutputBucketsAllowlist) {
		if b == bucket {
			return true
		}
	}
	return s.S3DefaultOutputBucket != "" && bucket == s.S3DefaultOutputBucket
}

// ResolveOutputTarget resolves an s3_output URL to (bucket, key). A bucket-only
// URL falls back to the configured key template with {request_id} substituted.
// Mirrors resolve_output_target.
func ResolveOutputTarget(url, requestID string, s *config.Settings) (bucket, key string, err error) {
	bucket, key, err = ParseURL(url)
	if err != nil {
		return "", "", err
	}
	if key == "" {
		key = strings.ReplaceAll(s.S3OutputKeyTemplate, "{request_id}", requestID)
	}
	return bucket, key, nil
}

// Ops is the network-facing S3 surface. The default implementation (aws.go)
// returns a clear error; a real aws-sdk-go-v2 implementation can be slotted in
// without touching callers. All three enforce their allowlist BEFORE any call.
type Ops interface {
	// DownloadToPath streams an S3 object to dest, returning its SHA-256 hex.
	DownloadToPath(url, dest string, s *config.Settings) (string, error)
	// UploadFile uploads a local PDF to s3://bucket/key.
	UploadFile(localPath, bucket, key string, s *config.Settings) error
	// PresignGetURL mints a short-TTL presigned GET URL.
	PresignGetURL(bucket, key string, s *config.Settings) (string, error)
}
