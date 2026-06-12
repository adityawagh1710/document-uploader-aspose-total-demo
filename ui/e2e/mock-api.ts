// Installs deterministic /api/** route handlers so the mocked E2E specs run
// against `next dev` alone — no Go backend, no Aspose license, no Gotenberg.
// All browser calls go through the /api/* single-origin proxy (BR-UI-1), so
// intercepting '**/api/**' covers every request the UI makes.

import type { Page, Route } from '@playwright/test';
import * as fx from './fixtures';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

export interface MockOptions {
  // Drive the HistoryPanel stale-cursor reset path.
  staleHistory?: boolean;
  // Make the Aspose HTML engine succeed too (default: 503 license_expired,
  // mirroring the real expired-license behavior).
  asposeSucceeds?: boolean;
}

export async function mockApi(page: Page, opts: MockOptions = {}): Promise<void> {
  // Regex (not a glob) — globs mis-handle the `?` in query strings like
  // /api/v1/conversions?cursor=… and silently fail to intercept.
  await page.route(/\/api\//, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, '');
    const method = route.request().method();

    // ---- HTML dual-engine conversion ----
    if (method === 'POST' && path === '/v1/convert/html/gotenberg') {
      return route.fulfill({
        status: 200,
        contentType: 'application/pdf',
        headers: { 'x-request-id': 'req_gotenberg_live' },
        body: fx.fakePdf,
      });
    }
    if (method === 'POST' && path === '/v1/convert/html/aspose') {
      if (opts.asposeSucceeds) {
        return route.fulfill({
          status: 200,
          contentType: 'application/pdf',
          headers: { 'x-request-id': 'req_aspose_live' },
          body: fx.fakePdf,
        });
      }
      return json(route, fx.licenseExpiredDiagnostic, 503);
    }

    // ---- GET telemetry / polling endpoints ----
    if (path === '/health') return json(route, fx.health);
    if (path === '/v1/stats') return json(route, fx.containerStats);
    if (path === '/v1/workers') return json(route, fx.workers);
    if (path === '/v1/conversions/stats') return json(route, fx.conversionsStats);
    if (path.startsWith('/v1/conversions')) {
      return json(route, opts.staleHistory ? fx.stalePage : fx.conversionsPage);
    }
    if (path.startsWith('/v1/downloads/presign')) return json(route, fx.presigned);

    // Unmatched API call — fail loudly so a missing mock is obvious.
    return json(route, { failure_class: 'unmocked', path }, 501);
  });
}
