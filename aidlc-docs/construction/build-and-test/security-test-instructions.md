# Security Test Instructions — office-converter (Local v1)

Required because the `security-baseline` extension is **Enabled** as a
blocking constraint. v1's security posture is "local-only trust
boundary; no app-layer auth"; the tests below verify the negative
guarantees (no secrets leak, no privileged execution, input validation
works as designed).

## Test Categories

### 1. Static — dependency vulnerability scan

```bash
uv pip install pip-audit
pip-audit --requirement <(uv pip compile pyproject.toml -)
```

Or run with `safety`:

```bash
uv pip install safety
safety check --requirement <(uv pip compile pyproject.toml -)
```

**Expected**: zero HIGH or CRITICAL findings. Acceptable: documented
unfixable advisories (record in `aidlc-docs/construction/office-converter/code/known-limitations.md`).

### 2. Static — secrets-in-source scan

```bash
docker run --rm -v "$PWD:/src" \
    trufflesecurity/trufflehog:latest filesystem /src --no-update
```

Or `gitleaks`:

```bash
gitleaks detect --source . --no-banner
```

**Expected**: zero matches. The repo MUST NOT contain:

- `.lic` files (Aspose licenses are operator-supplied via bind-mount)
- `aspose-total-cpp.tar.gz` (operator-supplied)
- API keys, tokens, passwords of any kind

Both files are in `.gitignore` already; this test enforces it.

### 3. Static — image hardening scan

```bash
docker scout cves office-convert:dev          # Docker's built-in CVE scan
trivy image office-convert:dev                # Aqua's Trivy
```

**Expected**: zero CRITICAL CVEs from the runtime base image
(`python:3.11-slim-bookworm`). Operator updates the base image and
rebuilds when CVE patches are published.

### 4. Container runtime — non-root user

```bash
docker run --rm office-convert:dev id
# Expected: uid=1000(appuser) gid=1000(appgroup)
```

```bash
docker inspect office-convert:dev --format='{{.Config.User}}'
# Expected: appuser
```

**Expected**: no process runs as root.

### 5. Container runtime — read-only root filesystem

```bash
docker run --rm --read-only --tmpfs /tmp --tmpfs /var/run \
    office-convert:dev \
    sh -c 'touch /test 2>&1 || echo "rootfs read-only OK"'
# Expected: "rootfs read-only OK" (touch fails with EROFS)
```

**Expected**: image runs successfully with `--read-only` + tmpfs.

### 6. Container runtime — dropped capabilities

```bash
docker run --rm --cap-drop=ALL \
    -v $(pwd)/license.lic:/aspose/license.lic:ro \
    office-convert:dev \
    sh -c 'uvicorn office_convert.server:app --host 127.0.0.1 --port 8080 &
           sleep 5; curl -fsS http://127.0.0.1:8080/health'
# Expected: /health returns 200 with the server still functional
```

**Expected**: image runs successfully without any Linux capabilities.

### 7. Application — input validation

Already covered by `tests/unit/test_probe.py` and
`tests/property/test_format_detection_pbt.py`. To run as a security
spot-check:

```bash
pytest tests/property/test_format_detection_pbt.py -v
```

**Expected**: 100 Hypothesis examples per property; random byte
sequences are rejected with `UnsupportedFormatError` (proves no
magic-byte bypass).

### 8. Application — no document content in logs

Manual verification — submit a document with distinctive content,
inspect logs:

```bash
# Real container, sample DOCX containing string "S3CR3T-SENTINEL-XYZ"
docker run -d --name oc-test -p 127.0.0.1:8080:8080 \
    -v $(pwd)/license.lic:/aspose/license.lic:ro \
    office-convert:dev

curl -X POST http://127.0.0.1:8080/convert \
    -F "file=@secret-doc.docx" -o /tmp/out.pdf

docker logs oc-test 2>&1 | grep -F "S3CR3T-SENTINEL-XYZ" \
    && echo "FAIL: document content in logs" \
    || echo "PASS: document content not in logs"

docker rm -f oc-test
```

**Expected**: PASS — no document content in stdout/stderr.

### 9. Application — no license content in logs

Manual verification. The `LicenseManager.expiry_date()` error path
might inadvertently leak XML content; verify by triggering a license
parse error:

```bash
docker run -d --name oc-bad-lic -p 127.0.0.1:8080:8080 \
    -v /dev/null:/aspose/license.lic:ro \
    office-convert:dev

curl -fsS http://127.0.0.1:8080/health
docker logs oc-bad-lic 2>&1
docker rm -f oc-bad-lic
```

**Expected**: logs show `license_path_missing` or `license_invalid`
but DO NOT echo any license file contents.

### 10. Network — service binds only where operator chose

The image's default `CMD` binds `0.0.0.0:8080`. The operator's run
command controls host-side exposure. Recommended posture:

```bash
docker run -p 127.0.0.1:8080:8080 ...      # localhost-only — recommended
docker run -p 0.0.0.0:8080:8080 ...        # any-interface — explicit operator choice
docker run -p 192.168.1.5:8080:8080 ...    # specific-interface — explicit operator choice
```

**Expected**: README documents the localhost-only recommendation.
This test is a smoke for that documentation, not an automated check.

## Pass / Fail Criteria

| Category | Threshold |
| -------- | --------- |
| Dependency CVEs (HIGH+) | 0 |
| Secrets in source | 0 |
| Image CVEs (CRITICAL) | 0 |
| Non-root verification | Pass |
| Read-only root verification | Pass |
| Cap-drop verification | Pass |
| Format-detection PBT | Pass |
| Document content in logs | Not present (manual sentinel test) |
| License content in logs | Not present (manual trigger) |

## CI Integration

Bundle the static checks (`pip-audit`, `gitleaks`, `trivy`) into a
single CI job that runs on every PR. The runtime checks (non-root,
read-only, cap-drop) belong with the e2e suite. The manual sentinel
tests run pre-release.
