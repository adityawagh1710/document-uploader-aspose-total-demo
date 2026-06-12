import { NextRequest, NextResponse } from 'next/server';

// Per-request nonce CSP (BR-UI-7 / SECURITY-04).
//
// WHY MIDDLEWARE, NOT next.config headers(): Next.js App Router streams its
// hydration / RSC payload via INLINE <script> tags (self.__next_f.push(…) and
// the bootstrap). A static `script-src 'self'` (no 'unsafe-inline', no nonce)
// blocks those — the page renders SSR HTML but never hydrates (no client React,
// no SWR, no interactivity). The fix is a per-request nonce: Next.js detects a
// `nonce-…` in the CSP request header and stamps it onto every inline/chunk
// script it emits, so the browser executes exactly those and nothing else.
// 'strict-dynamic' lets the nonced bootstrap load the same-origin chunk files.

const DASHBOARD_ORIGIN = (() => {
  try {
    return new URL(
      process.env.NEXT_PUBLIC_DASHBOARD_URL ?? 'http://localhost:8080/v1/dashboard',
    ).origin;
  } catch {
    return 'http://localhost:8080';
  }
})();

export function middleware(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());

  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    `frame-src ${DASHBOARD_ORIGIN}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join('; ');

  // Pass the nonce + CSP into the render so Next.js nonces its own scripts.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);
  requestHeaders.set('content-security-policy', csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  // And set it on the response so the browser actually enforces it.
  response.headers.set('content-security-policy', csp);
  return response;
}

// Run on all routes except Next's static assets and the favicon (those are
// served as static files and don't need the per-request nonce machinery).
export const config = {
  matcher: [
    {
      source: '/((?!_next/static|_next/image|favicon.ico).*)',
      missing: [
        { type: 'header', key: 'next-router-prefetch' },
        { type: 'header', key: 'purpose', value: 'prefetch' },
      ],
    },
  ],
};
