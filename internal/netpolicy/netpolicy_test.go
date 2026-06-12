package netpolicy

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestDeniedTable(t *testing.T) {
	denied := []string{
		"http://localhost/x.png",
		"http://sub.localhost/x.png",
		"http://localhost:8080/x.png",
		"http://127.0.0.1/style.css",
		"http://127.8.9.10/style.css",
		"http://0.0.0.0/",
		"http://10.0.0.5/a",
		"http://172.16.0.1/a",
		"http://172.31.255.255/a",
		"http://192.168.8.24/a",
		"http://169.254.169.254/latest/meta-data/", // the metadata endpoint
		"http://[::1]/a",
		"http://[fd00::1]/a",
		"http://[fe80::1]/a",
		"http://localstack:4566/bucket/key", // single-label in-cluster name
		"http://gotenberg:3000/health",
		"ftp://example.com/file",
		"file:///etc/passwd",
	}
	for _, u := range denied {
		got, reason := Denied(u)
		assert.Truef(t, got, "expected DENY for %s", u)
		assert.NotEmptyf(t, reason, "deny reason missing for %s", u)
	}

	allowed := []string{
		"https://cdn.jsdelivr.net/npm/chart.js",
		"http://fonts.googleapis.com/css?family=Roboto",
		"https://example.com/logo.png",
		"https://8.8.8.8/x.png", // public IP literal
		"http://172.32.0.1/a",   // just outside 172.16/12
		"http://192.169.0.1/a",  // just outside 192.168/16
		"data:image/png;base64,iVBORw0KGgo=",
	}
	for _, u := range allowed {
		got, _ := Denied(u)
		assert.Falsef(t, got, "expected ALLOW for %s", u)
	}
}
