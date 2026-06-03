package main

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestHealthcheck(t *testing.T) {
	ready := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer ready.Close()
	require.Equal(t, 0, healthcheck(ready.URL), "200 -> exit 0")

	notReady := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer notReady.Close()
	require.Equal(t, 1, healthcheck(notReady.URL), "503 -> exit 1")

	// Unreachable server -> exit 1 (no panic).
	require.Equal(t, 1, healthcheck("http://127.0.0.1:1/health"), "connection refused -> exit 1")
}

func TestHealthcheckURLDefaultPort(t *testing.T) {
	t.Setenv("OFFICE_CONVERT_PORT", "")
	require.Equal(t, "http://localhost:8080/health", healthcheckURL())
	t.Setenv("OFFICE_CONVERT_PORT", "9090")
	require.Equal(t, "http://localhost:9090/health", healthcheckURL())
}
