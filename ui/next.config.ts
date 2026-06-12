import type { NextConfig } from 'next';

// API_URL is the SERVER-side Go API target for the /api/* rewrite proxy
// (BR-UI-1: the browser only ever talks to this UI's origin). The default is
// the compose service name; override via build arg / env for other topologies.
// NOTE: next.config values are serialized into the standalone build, so the
// effective value is fixed at `next build` time — pass --build-arg API_URL=…
// in ui/Dockerfile if the target differs from the compose default.
const API_URL = process.env.API_URL ?? 'http://office-convert:8080';

// NOTE: the dashboard-iframe frame-src origin moved to middleware.ts together
// with the rest of the (now nonce-based) CSP.

const nextConfig: NextConfig = {
  output: 'standalone',
  // Pin the tracing root to this app dir — stray lockfiles outside the repo
  // otherwise make Next mis-infer the workspace root, which breaks the
  // standalone output layout the Dockerfile copies.
  outputFileTracingRoot: __dirname,
  poweredByHeader: false,

  async rewrites() {
    return [{ source: '/api/:path*', destination: `${API_URL}/:path*` }];
  },

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
