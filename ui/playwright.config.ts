import { defineConfig, devices } from '@playwright/test';

// Two modes:
//   • Mocked (default): start `next dev` and intercept /api/** with fixtures
//     (see e2e/mock-api.ts). Deterministic, no backend, CI-friendly.
//   • Live (E2E_LIVE=1): point at the running container at :8501 and hit the
//     real Go API + Gotenberg. No webServer is started.
const LIVE = process.env.E2E_LIVE === '1';
const PORT = Number(process.env.E2E_PORT ?? (LIVE ? 8501 : 3100));
const baseURL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  // Only spin up a server for the mocked run. In live mode the stack is
  // expected to already be up (docker compose up -d).
  //
  // We run the PRODUCTION standalone server, NOT `next dev` and NOT `next start`:
  //   • `next dev` uses eval/inline HMR scripts that the production CSP
  //     (`script-src 'self'`, BR-UI-7) correctly blocks → client never hydrates.
  //   • `next start` "does not work with output: standalone" (static chunks
  //     aren't served) → also no hydration.
  // `node .next/standalone/server.js` (after copying static in, exactly like
  // ui/Dockerfile) is what the container and real users run. CSP-accurate.
  webServer: LIVE
    ? undefined
    : {
        command: `npm run build && cp -r .next/static .next/standalone/.next/static && PORT=${PORT} node .next/standalone/server.js`,
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 180_000,
      },
});
