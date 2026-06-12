'use client';

import useSWR from 'swr';
import { Card } from '@/components/ui/Card';
import { fetcher } from '@/lib/api';
import { formatBytes } from '@/lib/format';
import type { ContainerStats, Health, WorkerProc } from '@/lib/types';

function Tile({
  label,
  value,
  sub,
  testId,
  stagger,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  testId: string;
  stagger?: string;
}) {
  return (
    <Card className={`p-4 ${stagger ?? ''}`}>
      <p className="text-xs text-slate-500">{label}</p>
      <p data-testid={testId} className="mt-1 font-mono text-2xl font-bold text-slate-100">
        {value}
      </p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </Card>
  );
}

export function HealthTiles() {
  // BR-UI-4: health/stats poll at 3 s; SWR pauses polling on hidden tabs.
  const { data: health } = useSWR<Health>('/health', fetcher, { refreshInterval: 3000 });
  const { data: stats } = useSWR<ContainerStats>('/v1/stats', fetcher, { refreshInterval: 3000 });
  const { data: workers } = useSWR<{ workers: WorkerProc[] }>('/v1/workers', fetcher, {
    refreshInterval: 3000,
  });

  const memPct =
    stats && stats.mem_max_bytes > 0
      ? `${((stats.mem_bytes / stats.mem_max_bytes) * 100).toFixed(0)}%`
      : '—';

  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
      <Tile
        label="Service"
        testId="tile-ready"
        stagger="stagger-1"
        value={
          health ? (
            <span className={health.ready ? 'text-emerald-400' : 'text-rose-400'}>
              {health.ready ? 'READY' : 'NOT READY'}
            </span>
          ) : (
            '…'
          )
        }
        sub={health?.problems.length ? health.problems.join('; ') : undefined}
      />
      <Tile
        label="Active jobs"
        testId="tile-jobs"
        stagger="stagger-2"
        value={health ? `${health.active_jobs}/${health.max_jobs}` : '…'}
      />
      <Tile
        label="License"
        testId="tile-license"
        stagger="stagger-3"
        value={
          health?.license_days_remaining == null ? (
            '—'
          ) : health.license_days_remaining < 0 ? (
            <span className="text-rose-400">EXPIRED</span>
          ) : (
            `${health.license_days_remaining}d`
          )
        }
        sub={
          health?.license_days_remaining != null && health.license_days_remaining < 0
            ? `${-health.license_days_remaining}d ago`
            : 'days remaining'
        }
      />
      <Tile
        label="Memory"
        testId="tile-memory"
        stagger="stagger-4"
        value={memPct}
        sub={
          stats
            ? `${formatBytes(stats.mem_bytes)} / ${formatBytes(stats.mem_max_bytes)}`
            : undefined
        }
      />
      <Tile
        label="Workers"
        testId="tile-workers"
        stagger="stagger-5"
        value={workers ? workers.workers.length : '…'}
        sub={
          workers && workers.workers.length > 0
            ? `${formatBytes(workers.workers.reduce((a, w) => a + w.rss_bytes, 0))} RSS`
            : 'aspose subprocesses'
        }
      />
    </div>
  );
}
