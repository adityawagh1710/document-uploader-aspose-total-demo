import type { NextConfig } from 'next';

// NOTE: the /api/* → Go API proxy is NO LONGER a next.config rewrite. rewrites()
// buffered large multipart uploads and timed out at ~30s (HTTP 500 on big
// files). It's now a streaming Node route handler at app/api/[...path]/route.ts,
// which reads API_URL at runtime. The dashboard-iframe frame-src origin lives in
// middleware.ts with the rest of the (nonce-based) CSP.

const nextConfig: NextConfig = {
  output: 'standalone',
  // Pin the tracing root to this app dir — stray lockfiles outside the repo
  // otherwise make Next mis-infer the workspace root, which breaks the
  // standalone output layout the Dockerfile copies.
  outputFileTracingRoot: __dirname,
  poweredByHeader: false,

  // BR-UI-7 / SECURITY-04 static header set. The Content-Security-Policy is NOT
  // here — it needs a per-request nonce so Next.js App Router's inline bootstrap
  // / RSC-streaming scripts execute (a static `script-src 'self'` blocks them and
  // the app never hydrates). The CSP is set in middleware.ts. These remaining
  // headers are request-independent and fine to set statically.
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
        ],
      },
    ];
  },
};

export default nextConfig;
