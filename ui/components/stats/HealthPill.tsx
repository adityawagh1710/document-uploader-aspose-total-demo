'use client';

import useSWR from 'swr';
import clsx from 'clsx';
import { fetchHealth } from '@/lib/api';
import type { Health } from '@/lib/types';

// Header health indicator. SWR polls /health every 3 s (BR-UI-4). fetchHealth
// returns the Health JSON for BOTH 200 (ready) and 503 (not-ready), so the
// error slot is reached ONLY on a real network failure → that's the genuine
// "API DOWN". A 503 not-ready (e.g. license_expired) shows as NOT READY.
export function HealthPill() {
  const { data, error } = useSWR<Health>('/health', fetchHealth, { refreshInterval: 3000 });

  const state = error ? 'down' : !data ? 'loading' : data.ready ? 'ready' : 'not-ready';
  const dot = {
    // `live-dot` adds the expanding ping ring — a visible heartbeat that the
    // dashboard is actively polling. Steady states (down/not-ready) don't ping.
    ready: 'bg-emerald-400 live-dot',
    'not-ready': 'bg-amber-400',
    down: 'bg-rose-500',
    loading: 'bg-slate-500 animate-pulse',
  }[state];
  const label = {
    ready: 'READY',
    'not-ready': 'NOT READY',
    down: 'API DOWN',
    loading: '…',
  }[state];

  return (
    <span
      data-testid="health-pill"
      className="inline-flex items-center gap-2 rounded-full border border-surface-edge px-3 py-1 font-mono text-xs"
    >
      <span className={clsx('h-2 w-2 rounded-full', dot)} />
      {label}
      {data?.license_days_remaining != null && (
        <span className={clsx(data.license_days_remaining < 0 ? 'text-rose-400' : 'text-slate-500')}>
          · lic {data.license_days_remaining < 0 ? 'expired' : `${data.license_days_remaining}d`}
        </span>
      )}
    </span>
  );
}
