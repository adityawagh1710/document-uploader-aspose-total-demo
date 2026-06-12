import { test, expect } from '@playwright/test';
import { mockApi } from './mock-api';

// Mocked dashboard rendering: all five surfaces present, health tiles populate
// from the proxied /api/health + /api/v1/stats, and the dashboard iframe + CSP
// are wired. No backend required.
test.describe('dashboard shell', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
    await page.goto('/');
  });

  test('renders the five dashboard sections', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Convert a document' })).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'HTML → PDF · engine comparison' }),
    ).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Conversion history' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Performance' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Live dashboard' })).toBeVisible();
  });

  test('health tiles populate from the API', async ({ page }) => {
    await expect(page.getByTestId('tile-ready')).toContainText('READY');
    await expect(page.getByTestId('tile-jobs')).toContainText('0/2');
    await expect(page.getByTestId('tile-license')).toContainText('23d');
  });

  test('embeds the live dashboard iframe', async ({ page }) => {
    const frame = page.getByTestId('dashboard-iframe');
    await expect(frame).toBeVisible();
    await expect(frame).toHaveAttribute('src', /\/v1\/dashboard$/);
  });

  test('serves a Content-Security-Policy header (BR-UI-7)', async ({ page }) => {
    const res = await page.request.get('/');
    const csp = res.headers()['content-security-policy'];
    expect(csp).toBeTruthy();
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("object-src 'none'");
  });
});
