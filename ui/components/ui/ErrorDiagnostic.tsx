import { Badge } from './Badge';
import type { Diagnostic } from '@/lib/types';

// BR-UI-2: API errors are rendered as TEXT (failure_class chip + key/value
// detail). API-sourced strings are never injected as HTML.
export function ErrorDiagnostic({ error }: { error: Diagnostic | string }) {
  if (typeof error === 'string') {
    return (
      <div data-testid="error-diagnostic" className="rounded-lg bg-rose-500/10 p-3 text-sm text-rose-300">
        {error}
      </div>
    );
  }
  const entries = Object.entries(error.detail ?? {});
  return (
    <div data-testid="error-diagnostic" className="rounded-lg bg-rose-500/10 p-3">
      <div className="flex items-center gap-2">
        <Badge tone="err" testId="failure-class-chip">
          {error.failure_class}
        </Badge>
        <span className="font-mono text-xs text-slate-500">req {error.request_id}</span>
      </div>
      {entries.length > 0 && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-mono text-slate-500">{k}</dt>
              <dd className="break-all text-slate-300">
                {typeof v === 'string' ? v : JSON.stringify(v)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
