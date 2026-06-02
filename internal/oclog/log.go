// Package oclog is structured logging + the request-id helper.
//
// Ported from office_convert/logging.py. Implements FR-10. The Python original
// uses a ContextVar to propagate the request id through asyncio tasks; in Go we
// thread it through context.Context (WithRequestID / RequestID) and pools carry
// it explicitly. EmitEvent mirrors emit_event's structured-event vocabulary.
package oclog

import (
	"context"
	"log/slog"
	"os"
	"strings"
)

type ctxKey struct{}

// NoRequestID is the sentinel used when no request id is bound (matches the
// Python ContextVar default "-").
const NoRequestID = "-"

// WithRequestID returns a context carrying the request id.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, ctxKey{}, id)
}

// RequestID extracts the bound request id, or "-" if none.
func RequestID(ctx context.Context) string {
	if v, ok := ctx.Value(ctxKey{}).(string); ok && v != "" {
		return v
	}
	return NoRequestID
}

var levelMap = map[string]slog.Level{
	"debug":   slog.LevelDebug,
	"info":    slog.LevelInfo,
	"warn":    slog.LevelWarn,
	"warning": slog.LevelWarn,
	"error":   slog.LevelError,
}

// Configure installs the root slog handler. Called once at server startup.
// format is "json" or "human"; level is debug|info|warn|error.
func Configure(format, level string) {
	lvl, ok := levelMap[strings.ToLower(level)]
	if !ok {
		lvl = slog.LevelInfo
	}
	opts := &slog.HandlerOptions{Level: lvl}
	var h slog.Handler
	if format == "human" {
		h = slog.NewTextHandler(os.Stderr, opts)
	} else {
		h = slog.NewJSONHandler(os.Stderr, opts)
	}
	slog.SetDefault(slog.New(h))
}

// EmitEvent emits a structured log event using the canonical event vocabulary.
// Mirrors emit_event(event, level, **fields).
func EmitEvent(level, event string, fields map[string]any) {
	attrs := make([]any, 0, len(fields)*2)
	for k, v := range fields {
		attrs = append(attrs, slog.Any(k, v))
	}
	switch strings.ToLower(level) {
	case "debug":
		slog.Debug(event, attrs...)
	case "warn", "warning":
		slog.Warn(event, attrs...)
	case "error":
		slog.Error(event, attrs...)
	default:
		slog.Info(event, attrs...)
	}
}
