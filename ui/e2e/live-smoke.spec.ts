import { test, expect } from '@playwright/test';

// OPT-IN live smoke against the REAL running stack (docker compose up).
// Run with:  E2E_LIVE=1 npm --prefix ui run test:e2e:live
// Skipped by default (and in CI) — it needs the Go API + Gotenberg up at :8501.
// This is the only spec that exercises the real /api/* proxy → Go API → Gotenberg
// path end-to-end (license-independent; Gotenberg is Chromium, no Aspose).
const LIVE = process.env.E2E_LIVE === '1';

test.describe('live stack smoke', () => {
  test.skip(!LIVE, 'set E2E_LIVE=1 and `docker compose up -d` to run');

  test('UI loads and health tiles reflect the real API', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByTestId('tile-ready')).toContainText('READY');
  });

  test('real Gotenberg HTML conversion via the UI proxy produces a PDF', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('compare-file-input').setInputFiles({
      name: 'sample.html',
      mimeType: 'text/html',
      buffer: Buffer.from(
        '<!doctype html><html><head><title>e2e</title></head><body><h1>live smoke</h1></body></html>',
      ),
    });
    await page.getByTestId('compare-both-button').click();

    // Results render only after BOTH engines settle (Promise.all). Cold
    // Chromium can take several seconds, so allow up to 30s for the cards.
    await expect(page.getByTestId('compare-results')).toBeVisible({ timeout: 30_000 });
    // Gotenberg (no Aspose license needed) must produce a downloadable PDF.
    await expect(page.getByTestId('engine-card-gotenberg')).toBeVisible();
    await expect(page.getByTestId('engine-download-gotenberg')).toBeVisible({ timeout: 30_000 });
    // Aspose path is expected to fail on the expired real license — its card
    // still renders (independence), just without a download link.
    await expect(page.getByTestId('engine-card-aspose')).toBeVisible();
  });
});
