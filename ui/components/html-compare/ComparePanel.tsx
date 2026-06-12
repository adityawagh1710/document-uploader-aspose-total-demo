'use client';

import { useState } from 'react';
import useSWR from 'swr';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';
import { Card } from '@/components/ui/Card';
import { Spinner } from '@/components/ui/Spinner';
import { EngineCard } from './EngineCard';
import {
  convertHTML,
  fetcher,
  HTML_MAX_BYTES,
  WAIT_DELAY_MAX_SECONDS,
  WAIT_DELAY_PATTERN,
} from '@/lib/api';
import { formatBytes, formatMs } from '@/lib/format';
import type { ConversionsStats, EngineRunResult } from '@/lib/types';

const ENGINE_COLORS = { gotenberg: '#22d3ee', aspose: '#a78bfa' } as const;

function validWaitDelay(v: string): boolean {
  if (!v) return true;
  const m = WAIT_DELAY_PATTERN.exec(v);
  if (!m || !m[1] || !m[3]) return false;
  const n = parseFloat(m[1]);
  return (m[3] === 'ms' ? n / 1000 : n) <= WAIT_DELAY_MAX_SECONDS;
}

export function ComparePanel() {
  const [file, setFile] = useState<File | null>(null);
  const [waitDelay, setWaitDelay] = useState('');
  const [waitExpr, setWaitExpr] = useState('');
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<EngineRunResult[] | null>(null);

  const { data: stats } = useSWR<ConversionsStats>('/v1/conversions/stats', fetcher, {
    refreshInterval: 5000,
  });
  const perEngine = stats?.per_engine_html;

  const tooBig = file !== null && file.size > HTML_MAX_BYTES; // BR-UI-3 mirror
  const badDelay = !validWaitDelay(waitDelay.trim());
  const canRun = file !== null && !tooBig && !badDelay && !busy;

  async function runBoth() {
    if (!file) return;
    setBusy(true);
    setResults(null);
    const wait = { waitDelay: waitDelay.trim(), waitForExpression: waitExpr.trim() };
    // Parallel fire; convertHTML never throws, so one engine failing never
    // hides the other (independence by construction).
    const both = await Promise.all([
      convertHTML('gotenberg', file, wait),
      convertHTML('aspose', file, wait),
    ]);
    setResults(both);
    setBusy(false);
  }

  const chartData = results?.map((r) => ({
    engine: r.engine,
    latency: Math.round(r.latencyMs),
    ok: r.ok,
  }));

  return (
    <Card>
      <div className="flex flex-col gap-4">
        <input
          type="file"
          accept=".html,.htm"
          data-testid="compare-file-input"
          aria-label="HTML file to compare"
          className="text-sm text-slate-400 file:mr-3 file:rounded-lg file:border-0 file:bg-surface-edge file:px-3 file:py-1.5 file:text-sm file:text-slate-200 hover:file:bg-slate-700"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setResults(null);
          }}
        />
        {file && (
          <p className="text-xs text-slate-500">
            {file.name} · {formatBytes(file.size)}
            {tooBig && (
              <span className="ml-2 text-rose-400">exceeds the 10 MiB HTML cap</span>
            )}
          </p>
        )}

        <fieldset className="rounded-lg border border-surface-edge p-3">
          <legend className="px-1 text-xs text-slate-500">
            JS wait controls — Gotenberg only (Aspose has no JS engine)
          </legend>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-xs text-slate-400">
              waitDelay (e.g. 2s, max {WAIT_DELAY_MAX_SECONDS}s)
              <input
                type="text"
                value={waitDelay}
                placeholder="2s"
                data-testid="compare-wait-delay-input"
                className="input-dark"
                onChange={(e) => setWaitDelay(e.target.value)}
              />
              {badDelay && (
                <span className="text-rose-400">format: number + ms|s, ≤ 30s</span>
              )}
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-400">
              waitForExpression (JS, truthy = ready)
              <input
                type="text"
                value={waitExpr}
                placeholder='window.status === "ready"'
                data-testid="compare-wait-expression-input"
                className="input-dark"
                onChange={(e) => setWaitExpr(e.target.value)}
              />
            </label>
          </div>
        </fieldset>

        <div>
          <button
            type="button"
            className="btn-primary"
            data-testid="compare-both-button"
            disabled={!canRun}
            onClick={runBoth}
          >
            {busy ? 'Running both engines…' : 'Convert with BOTH engines'}
          </button>
        </div>

        {busy && <Spinner label="Running gotenberg + aspose in parallel" />}

        {results && file && (
          <>
            <div className="grid gap-3 sm:grid-cols-2" data-testid="compare-results">
              {results.map((r) => (
                <EngineCard key={r.engine} result={r} sourceName={file.name} />
              ))}
            </div>

            {chartData && (
              <div className="h-36">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 24 }}>
                    <XAxis
                      type="number"
                      tick={{ fill: '#64748b', fontSize: 11 }}
                      unit=" ms"
                      stroke="#1e293b"
                    />
                    <YAxis
                      type="category"
                      dataKey="engine"
                      width={80}
                      tick={{ fill: '#94a3b8', fontSize: 12 }}
                      stroke="#1e293b"
                    />
                    <Tooltip
                      formatter={(v) => [`${String(v)} ms`, 'latency']}
                      contentStyle={{ background: '#111a2c', border: '1px solid #1e293b' }}
                    />
                    <Bar dataKey="latency" radius={[0, 4, 4, 0]} barSize={18}>
                      {chartData.map((d) => (
                        <Cell
                          key={d.engine}
                          fill={d.ok ? ENGINE_COLORS[d.engine as 'gotenberg' | 'aspose'] : '#f43f5e'}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </>
        )}

        {perEngine && Object.keys(perEngine).length > 0 && (
          <div data-testid="per-engine-stats" className="border-t border-surface-edge pt-3">
            <p className="mb-2 text-xs text-slate-500">Cumulative (all HTML conversions)</p>
            <div className="grid grid-cols-2 gap-3">
              {(['gotenberg', 'aspose'] as const).map((eng) => {
                const t = perEngine[eng];
                if (!t) return null;
                return (
                  <div key={eng} className="font-mono text-xs text-slate-400">
                    <span style={{ color: ENGINE_COLORS[eng] }}>{eng}</span> · n={t.count} · avg{' '}
                    {formatMs(t.avg_ms)} · p95 {formatMs(t.p95_ms)}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
