import type { NextConfig } from 'next';

// API_URL is the SERVER-side Go API target for the /api/* rewrite proxy
// (BR-UI-1: the browser only ever talks to this UI's origin). The default is
// the compose service name; override via build arg / env for other topologies.
// NOTE: next.config values are serialized into the standalone build, so the
// effective value is fixed at `next build` time — pass --build-arg API_URL=…
// in ui/Dockerfile if the target differs from the compose default.
const API_URL = process.env.API_URL ?? 'http://office-convert:8080';

// Browser-facing API origin, used ONLY for the dashboard iframe (frame-src).
const DASHBOARD_ORIGIN = new URL(
  process.env.NEXT_PUBLIC_DASHBOARD_URL ?? 'http://localhost:8080/v1/dashboard',
).origin;

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

  // BR-UI-7 / SECURITY-04 header set. style-src 'unsafe-inline' is the
  // Tailwind/Next runtime reality; no inline scripts are emitted with the App
  // Router defaults used here, so script-src stays 'self'.
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          {
            key: 'Content-Security-Policy',
            value: [
              "default-src 'self'",
              "img-src 'self' data: blob:",
              "style-src 'self' 'unsafe-inline'",
              "script-src 'self'",
              "connect-src 'self'",
              `frame-src ${DASHBOARD_ORIGIN}`,
              "object-src 'none'",
              "base-uri 'self'",
            ].join('; '),
          },
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
