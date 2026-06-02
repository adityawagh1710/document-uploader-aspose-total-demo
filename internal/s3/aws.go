package s3

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/feature/s3/manager"
	awss3 "github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"

	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/oerrors"
)

// notFoundCodes / forbiddenCodes mirror s3_client.py's botocore code sets.
var notFoundCodes = map[string]bool{"NoSuchKey": true, "NoSuchBucket": true, "404": true, "NotFound": true}
var forbiddenCodes = map[string]bool{"AccessDenied": true, "403": true, "AllAccessDisabled": true}

// AWSOps is the aws-sdk-go-v2 implementation of Ops.
type AWSOps struct{}

// NewAWSOps returns the default AWS-backed Ops implementation.
func NewAWSOps() Ops { return AWSOps{} }

func loadClient(ctx context.Context, s *config.Settings) (*awss3.Client, error) {
	var opts []func(*awsconfig.LoadOptions) error
	if s.S3Region != "" {
		opts = append(opts, awsconfig.WithRegion(s.S3Region))
	}
	cfg, err := awsconfig.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		return nil, err
	}
	// AWS_ENDPOINT_URL_S3 / AWS_ENDPOINT_URL route to LocalStack with path-style.
	endpoint := firstNonEmpty(os.Getenv("AWS_ENDPOINT_URL_S3"), os.Getenv("AWS_ENDPOINT_URL"))
	return awss3.NewFromConfig(cfg, func(o *awss3.Options) {
		if endpoint != "" {
			o.BaseEndpoint = awssdk.String(endpoint)
			o.UsePathStyle = true
		} else if s.S3Region != "" {
			o.BaseEndpoint = awssdk.String("https://s3." + s.S3Region + ".amazonaws.com")
		}
	}), nil
}

// DownloadToPath streams s3://bucket/key to dest, returning its SHA-256 hex.
// Enforces the input allowlist before any S3 call. Mirrors download_to_path.
func (AWSOps) DownloadToPath(url, dest string, s *config.Settings) (string, error) {
	bucket, key, err := ParseURL(url)
	if err != nil {
		return "", err
	}
	if key == "" {
		return "", oerrors.NewS3InvalidURL(url)
	}
	if !IsInputBucketAllowed(bucket, s) {
		return "", oerrors.NewS3InputForbidden(bucket)
	}
	ctx := context.Background()
	client, err := loadClient(ctx, s)
	if err != nil {
		return "", oerrors.NewRender(nil, -1, "s3 config: "+err.Error())
	}
	obj, err := client.GetObject(ctx, &awss3.GetObjectInput{Bucket: &bucket, Key: &key})
	if err != nil {
		code := apiErrorCode(err)
		if notFoundCodes[code] {
			return "", oerrors.NewS3InputNotFound(bucket, key)
		}
		if forbiddenCodes[code] {
			return "", oerrors.NewS3InputForbidden(bucket)
		}
		return "", oerrors.NewRender(nil, -1, "s3 get: "+err.Error())
	}
	defer obj.Body.Close()
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", err
	}
	f, err := os.Create(dest)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(f, h), obj.Body); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// UploadFile uploads a local PDF to s3://bucket/key (multipart for large files
// via the transfer manager). Enforces the output allowlist first.
func (AWSOps) UploadFile(localPath, bucket, key string, s *config.Settings) error {
	if !IsOutputBucketAllowed(bucket, s) {
		return oerrors.NewS3OutputForbidden(bucket)
	}
	ctx := context.Background()
	client, err := loadClient(ctx, s)
	if err != nil {
		return oerrors.NewS3OutputUploadFailed(bucket, key, err.Error())
	}
	f, err := os.Open(localPath)
	if err != nil {
		return oerrors.NewS3OutputUploadFailed(bucket, key, err.Error())
	}
	defer f.Close()
	uploader := manager.NewUploader(client)
	ct := "application/pdf"
	_, err = uploader.Upload(ctx, &awss3.PutObjectInput{
		Bucket: &bucket, Key: &key, Body: f, ContentType: &ct,
	})
	if err != nil {
		return oerrors.NewS3OutputUploadFailed(bucket, key, err.Error())
	}
	return nil
}

// PresignGetURL mints a short-TTL presigned GET URL. Enforces the output
// allowlist BEFORE signing (without it the endpoint is a presigning oracle).
func (AWSOps) PresignGetURL(bucket, key string, s *config.Settings) (string, error) {
	if !IsOutputBucketAllowed(bucket, s) {
		return "", oerrors.NewS3OutputForbidden(bucket)
	}
	ctx := context.Background()
	cfg, err := awsconfig.LoadDefaultConfig(ctx, regionOpts(s)...)
	if err != nil {
		return "", oerrors.NewRender(nil, -1, "s3 config: "+err.Error())
	}
	// s3_public_endpoint signs against the host the browser will follow.
	signEndpoint := s.S3PublicEndpoint
	client := awss3.NewFromConfig(cfg, func(o *awss3.Options) {
		if signEndpoint != "" {
			o.BaseEndpoint = awssdk.String(signEndpoint)
			o.UsePathStyle = true
		} else if s.S3Region != "" {
			o.BaseEndpoint = awssdk.String("https://s3." + s.S3Region + ".amazonaws.com")
		}
	})
	ps := awss3.NewPresignClient(client)
	req, err := ps.PresignGetObject(ctx, &awss3.GetObjectInput{Bucket: &bucket, Key: &key},
		awss3.WithPresignExpires(time.Duration(s.S3PresignTTLSeconds)*time.Second))
	if err != nil {
		return "", oerrors.NewRender(nil, -1, "s3 presign: "+err.Error())
	}
	return req.URL, nil
}

func regionOpts(s *config.Settings) []func(*awsconfig.LoadOptions) error {
	if s.S3Region != "" {
		return []func(*awsconfig.LoadOptions) error{awsconfig.WithRegion(s.S3Region)}
	}
	return nil
}

func apiErrorCode(err error) string {
	var ae smithy.APIError
	if errors.As(err, &ae) {
		return ae.ErrorCode()
	}
	return ""
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
