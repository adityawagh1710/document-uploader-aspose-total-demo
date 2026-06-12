'use client';

import useSWR from 'swr';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import { Card } from '@/components/ui/Card';
import { fetcher } from '@/lib/api';
import { formatMs } from '@/lib/format';
import type { ConversionsStats } from '@/lib/types';

export function PerfPanel() {
  const { data } = useSWR<ConversionsStats>('/v1/conversions/stats', fetcher, {
    refreshInterval: 5000,
  });

  if (!data || data.totals.count === 0) {
    return (
      <Card>
        <p className="py-6 text-center text-sm text-slate-500">
          Awaiting conversion data — per-format and per-engine timings appear here.
        </p>
      </Card>
    );
  }

  const perFormat = Object.entries(data.per_format).map(([format, t]) => ({
    name: format,
    avg_ms: Math.round(t.avg_ms),
    p95_ms: Math.round(t.p95_ms),
    count: t.count,
  }));
  const perEngine = Object.entries(data.per_engine_html ?? {}).map(([engine, t]) => ({
    name: engine,
    avg_ms: Math.round(t.avg_ms),
    p95_ms: Math.round(t.p95_ms),
    count: t.count,
  }));

  const axisTick = { fill: '#64748b', fontSize: 11 };
  const tooltipStyle = { background: '#111a2c', border: '1px solid #1e293b' };

  return (
    <Card>
      <div className="mb-4 flex gap-6 font-mono text-sm">
        <span className="text-slate-300">
          total <strong>{data.totals.count}</strong>
        </span>
        <span className="text-emerald-400">
          ok <strong>{data.totals.successes}</strong>
        </span>
        <span className="text-rose-400">
          failed <strong>{data.totals.failures}</strong>
        </span>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div>
          <p className="mb-2 text-xs text-slate-500">Per format (avg vs p95, ms)</p>
          <div className="h-56" data-testid="per-format-chart">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={perFormat}>
                <XAxis dataKey="name" tick={axisTick} stroke="#1e293b" />
                <YAxis tick={axisTick} stroke="#1e293b" />
                <Tooltip
                  formatter={(v) => formatMs(Number(v))}
                  contentStyle={tooltipStyle}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="avg_ms" name="avg" fill="#22d3ee" radius={[3, 3, 0, 0]} />
                <Bar dataKey="p95_ms" name="p95" fill="#0e7490" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div>
          <p className="mb-2 text-xs text-slate-500">HTML per engine (avg vs p95, ms)</p>
          {perEngine.length > 0 ? (
            <div className="h-56" data-testid="per-engine-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={perEngine}>
                  <XAxis dataKey="name" tick={axisTick} stroke="#1e293b" />
                  <YAxis tick={axisTick} stroke="#1e293b" />
                  <Tooltip
                    formatter={(v) => formatMs(Number(v))}
                    contentStyle={tooltipStyle}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="avg_ms" name="avg" fill="#a78bfa" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="p95_ms" name="p95" fill="#6d28d9" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="flex h-56 items-center justify-center text-sm text-slate-600">
              No HTML conversions yet — use the engine comparison panel.
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}
