import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ComparePanel } from '../ComparePanel';

// recharts needs real layout measurement; stub the chart wrapper in jsdom.
vi.mock('recharts', async (importOriginal) => {
  const mod = await importOriginal<typeof import('recharts')>();
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="chart">{children}</div>
    ),
  };
});

function pdfResponse(): Response {
  return new Response(new Blob(['%PDF-1.7 fake'], { type: 'application/pdf' }), {
    status: 200,
    headers: { 'X-Request-ID': 'req-ok' },
  });
}

function diagnosticResponse(failureClass: string, status: number): Response {
  return new Response(
    JSON.stringify({ request_id: 'req-fail', failure_class: failureClass, detail: {} }),
    { status, headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-fail' } },
  );
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/v1/conversions/stats')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            per_format: {},
            per_engine_html: {
              gotenberg: { count: 3, avg_ms: 900, p95_ms: 1500 },
              aspose: { count: 3, avg_ms: 400, p95_ms: 600 },
            },
            totals: { count: 3, successes: 3, failures: 0 },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
    return Promise.resolve(pdfResponse());
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

async function uploadHTML(name = 'sample.html') {
  const input = screen.getByTestId('compare-file-input');
  const file = new File(['<html><body>hi</body></html>'], name, { type: 'text/html' });
  await userEvent.upload(input as HTMLInputElement, file);
}

describe('ComparePanel', () => {
  it('disables the run button until a file is chosen', () => {
    render(<ComparePanel />);
    expect(screen.getByTestId('compare-both-button')).toBeDisabled();
  });

  it('sends wait fields to gotenberg only (D4)', async () => {
    render(<ComparePanel />);
    await uploadHTML();
    await userEvent.type(screen.getByTestId('compare-wait-delay-input'), '2s');
    await userEvent.type(
      screen.getByTestId('compare-wait-expression-input'),
      'window.status === "ready"',
    );
    await userEvent.click(screen.getByTestId('compare-both-button'));

    await waitFor(() => expect(screen.getByTestId('compare-results')).toBeInTheDocument());

    const engineCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).includes('/v1/convert/html/'),
    );
    expect(engineCalls).toHaveLength(2);
    for (const [url, init] of engineCalls) {
      const body = (init as RequestInit).body as FormData;
      if (String(url).endsWith('/gotenberg')) {
        expect(body.get('waitDelay')).toBe('2s');
        expect(body.get('waitForExpression')).toBe('window.status === "ready"');
      } else {
        expect(String(url).endsWith('/aspose')).toBe(true);
        expect(body.get('waitDelay')).toBeNull();
        expect(body.get('waitForExpression')).toBeNull();
      }
    }
  });

  it('rejects an out-of-range waitDelay client-side (BR-UI-3)', async () => {
    render(<ComparePanel />);
    await uploadHTML();
    await userEvent.type(screen.getByTestId('compare-wait-delay-input'), '45s');
    expect(screen.getByTestId('compare-both-button')).toBeDisabled();
    expect(screen.getByText(/≤ 30s/)).toBeInTheDocument();
  });

  it('one engine failing never hides the other', async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/conversions/stats')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ per_format: {}, totals: { count: 0, successes: 0, failures: 0 } }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        );
      }
      if (url.endsWith('/gotenberg')) {
        return Promise.resolve(diagnosticResponse('engine_unavailable', 503));
      }
      return Promise.resolve(pdfResponse());
    });

    render(<ComparePanel />);
    await uploadHTML();
    await userEvent.click(screen.getByTestId('compare-both-button'));

    await waitFor(() => expect(screen.getByTestId('compare-results')).toBeInTheDocument());
    // Gotenberg card shows the diagnostic; Aspose card still offers the PDF.
    expect(screen.getByText('engine_unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('engine-download-aspose')).toBeInTheDocument();
    expect(screen.queryByTestId('engine-download-gotenberg')).not.toBeInTheDocument();
  });

  it('renders the cumulative per-engine stats row when the API has data', async () => {
    render(<ComparePanel />);
    await waitFor(() => expect(screen.getByTestId('per-engine-stats')).toBeInTheDocument());
    expect(screen.getByText('gotenberg')).toBeInTheDocument();
    expect(screen.getByText('aspose')).toBeInTheDocument();
  });
});
