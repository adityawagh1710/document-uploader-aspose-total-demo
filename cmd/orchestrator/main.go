// Command orchestrator is the Go office-convert orchestrator HTTP server.
//
// Equivalent of `uvicorn office_convert.server:app`. Wires config, logging,
// license, cache, observability stores, S3 ops, and the HTTP handler, then
// serves on :8080 (override with OFFICE_CONVERT_PORT).
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/opus2/office-convert-orchestrator/internal/cache"
	"github.com/opus2/office-convert-orchestrator/internal/config"
	"github.com/opus2/office-convert-orchestrator/internal/license"
	"github.com/opus2/office-convert-orchestrator/internal/obs"
	"github.com/opus2/office-convert-orchestrator/internal/oclog"
	"github.com/opus2/office-convert-orchestrator/internal/s3"
	"github.com/opus2/office-convert-orchestrator/internal/server"
	"github.com/opus2/office-convert-orchestrator/internal/worker"
)

func main() {
	// `orchestrator healthcheck` probes the running server's /health and exits
	// 0 if ready, else 1. Used by the Dockerfile HEALTHCHECK and the compose
	// healthcheck — the runtime image is Python-free, so the binary checks
	// itself (no curl/wget/python needed, works under a read-only rootfs).
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(healthcheck(healthcheckURL()))
	}

	s, err := config.Load()
	if err != nil {
		// Logging isn't configured yet; use stderr directly.
		slog.Error("config load failed", "err", err)
		os.Exit(1)
	}
	oclog.Configure(s.LogFormat, s.LogLevel)

	licMgr := license.NewManager(s.LicensePath)
	cacheMgr, err := cache.NewManager(s.CacheDir, s.AsposeVersion)
	if err != nil {
		slog.Error("cache init failed", "err", err)
		os.Exit(1)
	}
	stores := worker.Stores{
		Heartbeats: obs.HeartbeatStore(),
		Timings:    obs.TimingStore(),
		Progress:   obs.NewJobProgressStore(),
	}
	recent := obs.NewRecentStore(obs.DefaultMaxRecent)
	health := server.NewHealth(s, licMgr)
	srv := server.New(s, health, cacheMgr, stores, recent, s3.NewAWSOps(), server.DashboardHTML, server.LandingHTML)

	port := os.Getenv("OFFICE_CONVERT_PORT")
	if port == "" {
		port = "8080"
	}

	httpSrv := &http.Server{Addr: ":" + port, Handler: srv.Handler()}

	oclog.EmitEvent("info", "server_start", map[string]any{
		"max_jobs": s.MaxJobs, "parallel": s.Parallel, "port": port,
	})

	// Graceful shutdown on SIGINT/SIGTERM.
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		oclog.EmitEvent("info", "server_shutdown", nil)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = httpSrv.Shutdown(ctx)
	}()

	if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server failed", "err", err)
		os.Exit(1)
	}
}

// healthcheckURL builds the local /health URL from the configured port.
func healthcheckURL() string {
	port := os.Getenv("OFFICE_CONVERT_PORT")
	if port == "" {
		port = "8080"
	}
	return "http://localhost:" + port + "/health"
}

// healthcheck GETs url and returns a process exit code: 0 iff the server is
// ready (HTTP 200), else 1. /health returns 503 when not ready.
func healthcheck(url string) int {
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusOK {
		return 0
	}
	return 1
}
