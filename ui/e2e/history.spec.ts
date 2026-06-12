import { test, expect } from '@playwright/test';
import { mockApi } from './mock-api';

test.describe('conversion history (API-truth, BR-UI-5)', () => {
  test('renders rows with engine chips for HTML conversions', async ({ page }) => {
    await mockApi(page);
    await page.goto('/');

    await expect(page.getByTestId('history-table')).toBeVisible();
    await expect(page.getByTestId('history-row')).toHaveCount(3);
    // The gotenberg HTML row shows the engine badge; the cross-service docx row
    // has no engine field, so no badge.
    await expect(page.getByTestId('engine-badge-gotenberg')).toBeVisible();
    await expect(page.getByTestId('engine-badge-aspose')).toBeVisible();
  });

  test('failed rows surface the error code', async ({ page }) => {
    await mockApi(page);
    await page.goto('/');
    // The aspose row failed with license_expired.
    await expect(page.getByTestId('history-table')).toContainText('license_expired');
  });

  test('stale cursor resets pagination and shows the note', async ({ page }) => {
    await mockApi(page, { staleHistory: true });
    await page.goto('/');
    await expect(page.getByTestId('stale-cursor-note')).toBeVisible();
  });

  test('filter buttons are present and switchable', async ({ page }) => {
    await mockApi(page);
    await page.goto('/');
    await expect(page.getByTestId('history-filter-all')).toBeVisible();
    await page.getByTestId('history-filter-failed').click();
    // Still renders a table after switching filters (mock returns the same page).
    await expect(page.getByTestId('history-table')).toBeVisible();
  });
});
