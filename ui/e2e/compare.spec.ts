import { test, expect } from '@playwright/test';
import { mockApi } from './mock-api';

const HTML_SAMPLE = '<!doctype html><html><head><title>t</title></head><body><h1>hi</h1></body></html>';

async function chooseHtmlFile(page: import('@playwright/test').Page) {
  await page.getByTestId('compare-file-input').setInputFiles({
    name: 'sample.html',
    mimeType: 'text/html',
    buffer: Buffer.from(HTML_SAMPLE),
  });
}

test.describe('HTML engine comparison', () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
    await page.goto('/');
  });

  test('run button is disabled until a file is chosen', async ({ page }) => {
    await expect(page.getByTestId('compare-both-button')).toBeDisabled();
    await chooseHtmlFile(page);
    await expect(page.getByTestId('compare-both-button')).toBeEnabled();
  });

  test('engine independence: gotenberg succeeds even when aspose fails (503)', async ({ page }) => {
    await chooseHtmlFile(page);
    await page.getByTestId('compare-both-button').click();

    await expect(page.getByTestId('compare-results')).toBeVisible();
    // Both engine cards render side by side — one failing never hides the other.
    await expect(page.getByTestId('engine-card-gotenberg')).toBeVisible();
    await expect(page.getByTestId('engine-card-aspose')).toBeVisible();
    // Gotenberg produced a downloadable PDF; aspose surfaced its error.
    await expect(page.getByTestId('engine-download-gotenberg')).toBeVisible();
    await expect(page.getByTestId('engine-download-aspose')).toHaveCount(0);
  });

  test('client-side waitDelay bound blocks the run (BR-UI-3)', async ({ page }) => {
    await chooseHtmlFile(page);
    await page.getByTestId('compare-wait-delay-input').fill('45s'); // > 30s cap
    await expect(page.getByTestId('compare-both-button')).toBeDisabled();
    await page.getByTestId('compare-wait-delay-input').fill('2s');
    await expect(page.getByTestId('compare-both-button')).toBeEnabled();
  });

  test('cumulative per-engine stats render from the API', async ({ page }) => {
    await expect(page.getByTestId('per-engine-stats')).toBeVisible();
    await expect(page.getByTestId('per-engine-stats')).toContainText('gotenberg');
    await expect(page.getByTestId('per-engine-stats')).toContainText('n=2');
  });
});
