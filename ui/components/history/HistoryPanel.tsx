'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Card } from '@/components/ui/Card';
import { Badge, EngineBadge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { conversionsPath, fetcher } from '@/lib/api';
import { formatBytes, formatDate, formatMs } from '@/lib/format';
import type { ConversionsPage } from '@/lib/types';
import { PresignButton } from './PresignButton';

const FILTERS = ['all', 'ui', 'cross', 'failed'] as const;

// BR-UI-5 / D5: history is API-truth (/v1/conversions ring buffer) — no
// client-side store. Cross-service conversions (curl, classification fanout)
// are visible by construction.
export function HistoryPanel({ s3Enabled }: { s3Enabled: boolean }) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>('all');
  const [cursor, setCursor] = useState<string | null>(null);
  const [staleNote, setStaleNote] = useState(false);

  const { data, error, isLoading, mutate } = useSWR<ConversionsPage>(
    conversionsPath(cursor, 20, filter),
    fetcher,
    {
      refreshInterval: 5000,
      onSuccess: (page) => {
        // Stale cursor → the ring buffer rotated past our position; reset
        // pagination and tell the operator why the view jumped.
        if (page.stale_cursor) {
          setCursor(null);
          setStaleNote(true);
        }
      },
    },
  );

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1" role="group" aria-label="History filter">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              data-testid={`history-filter-${f}`}
              className={
                f === filter
                  ? 'rounded-lg bg-surface-edge px-3 py-1 text-xs font-semibold text-accent'
                  : 'rounded-lg px-3 py-1 text-xs text-slate-400 hover:text-slate-200'
              }
              onClick={() => {
                setFilter(f);
                setCursor(null);
                setStaleNote(false);
              }}
            >
              {f}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3">
          {data && (
            <span className="font-mono text-xs text-slate-500">buffer {data.buffer_size}</span>
          )}
          <button type="button" className="btn-ghost text-xs" onClick={() => mutate()}>
            ↻ refresh
          </button>
        </div>
      </div>

      {staleNote && (
        <p data-testid="stale-cursor-note" className="mb-2 rounded bg-amber-500/10 px-3 py-1.5 text-xs text-amber-400">
          The history buffer rotated past your page — view reset to the latest entries.
        </p>
      )}

      {isLoading && !data && <Spinner label="Loading history" />}
      {error && <p className="text-sm text-rose-400">History unavailable — is the API up?</p>}

      {data && data.entries.length === 0 && (
        <p className="py-6 text-center text-sm text-slate-500">
          No conversions recorded yet — convert something above.
        </p>
      )}

      {data && data.entries.length > 0 && (
        <div className="overflow-x-auto">
          <table data-testid="history-table" className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-surface-edge text-slate-500">
                <th className="py-2 pr-3 font-normal">time</th>
                <th className="py-2 pr-3 font-normal">file</th>
                <th className="py-2 pr-3 font-normal">format</th>
                <th className="py-2 pr-3 font-normal">engine</th>
                <th className="py-2 pr-3 font-normal">pages</th>
                <th className="py-2 pr-3 font-normal">duration</th>
                <th className="py-2 pr-3 font-normal">size</th>
                <th className="py-2 pr-3 font-normal">status</th>
                <th className="py-2 pr-3 font-normal">source</th>
                {s3Enabled && <th className="py-2 font-normal">s3</th>}
              </tr>
            </thead>
            <tbody>
              {data.entries.map((rec) => (
                <tr
                  key={rec.request_id}
                  data-testid="history-row"
                  className="border-b border-surface-edge/50 text-slate-300 transition-colors hover:bg-surface-edge/30"
                >
                  <td className="py-2 pr-3 font-mono text-slate-500">
                    {formatDate(rec.completion_ts)}
                  </td>
                  <td className="max-w-48 truncate py-2 pr-3" title={rec.input_filename ?? ''}>
                    {rec.input_filename ?? '—'}
                  </td>
                  <td className="py-2 pr-3 font-mono">{rec.format}</td>
                  <td className="py-2 pr-3">{rec.engine ? <EngineBadge engine={rec.engine} /> : null}</td>
                  <td className="py-2 pr-3 font-mono">{rec.page_count ?? '—'}</td>
                  <td className="py-2 pr-3 font-mono">{formatMs(rec.duration_ms)}</td>
                  <td className="py-2 pr-3 font-mono">{formatBytes(rec.output_size_bytes)}</td>
                  <td className="py-2 pr-3">
                    {rec.status === 'success' ? (
                      <Badge tone="ok">ok</Badge>
                    ) : (
                      <Badge tone="err">{rec.error_code ?? 'failed'}</Badge>
                    )}
                  </td>
                  <td className="py-2 pr-3 font-mono text-slate-500">{rec.source}</td>
                  {s3Enabled && (
                    <td className="py-2">
                      {rec.output_s3_uri ? <PresignButton s3Uri={rec.output_s3_uri} /> : null}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && (cursor !== null || data.has_more) && (
        <div className="mt-3 flex gap-2">
          {cursor !== null && (
            <button
              type="button"
              className="btn-ghost text-xs"
              data-testid="history-first-page"
              onClick={() => setCursor(null)}
            >
              ⏮ latest
            </button>
          )}
          {data.has_more && data.next_cursor && (
            <button
              type="button"
              className="btn-ghost text-xs"
              data-testid="history-next-page"
              onClick={() => setCursor(data.next_cursor)}
            >
              older →
            </button>
          )}
        </div>
      )}
    </Card>
  );
}
