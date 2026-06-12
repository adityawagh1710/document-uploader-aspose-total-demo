import { render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { SWRConfig } from 'swr';
import { HistoryPanel } from '../HistoryPanel';
import type { ConversionsPage, ConversionRecord } from '@/lib/types';

function record(over: Partial<ConversionRecord>): ConversionRecord {
  return {
    request_id: 'req-1',
    completion_ts: 1765000000,
    source: 'ui',
    input_filename: 'a.docx',
    format: 'docx',
    page_count: 3,
    duration_ms: 1200,
    status: 'success',
    error_code: null,
    output_s3_uri: null,
    output_size_bytes: 4096,
    ...over,
  };
}

function page(over: Partial<ConversionsPage>): ConversionsPage {
  return {
    entries: [],
    next_cursor: null,
    has_more: false,
    stale_cursor: false,
    buffer_size: 0,
    ...over,
  };
}

const fetchMock = vi.fn();

function renderPanel(s3 = false) {
  // provider: () => new Map() gives every test an isolated SWR cache.
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <HistoryPanel s3Enabled={s3} />
    </SWRConfig>,
  );
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

function respondWith(body: ConversionsPage) {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('HistoryPanel', () => {
  it('shows the engine chip only on records with an engine field', async () => {
    respondWith(
      page({
        entries: [
          record({ request_id: 'r1', format: 'html', engine: 'gotenberg' }),
          record({ request_id: 'r2', format: 'docx' }),
        ],
        buffer_size: 2,
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId('history-table')).toBeInTheDocument());
    expect(screen.getByTestId('engine-badge-gotenberg')).toBeInTheDocument();
    expect(screen.getAllByTestId('history-row')).toHaveLength(2);
    expect(screen.queryByTestId('engine-badge-aspose')).not.toBeInTheDocument();
  });

  it('resets pagination and explains when the cursor went stale', async () => {
    respondWith(page({ entries: [record({})], stale_cursor: true, buffer_size: 1 }));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId('stale-cursor-note')).toHaveTextContent(/rotated/),
    );
  });

  it('renders failed records with their error_code', async () => {
    respondWith(
      page({
        entries: [record({ status: 'failed', error_code: 'render_failed' })],
        buffer_size: 1,
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByText('render_failed')).toBeInTheDocument());
  });

  it('shows the presign button only for rows with an S3 output (s3 enabled)', async () => {
    respondWith(
      page({
        entries: [
          record({ request_id: 'r1', output_s3_uri: 's3://office-convert-out/a.pdf' }),
          record({ request_id: 'r2' }),
        ],
        buffer_size: 2,
      }),
    );
    renderPanel(true);

    await waitFor(() => expect(screen.getByTestId('history-table')).toBeInTheDocument());
    expect(screen.getAllByTestId('presign-button')).toHaveLength(1);
  });
});
