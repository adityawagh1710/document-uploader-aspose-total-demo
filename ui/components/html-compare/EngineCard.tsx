'use client';

import { EngineBadge } from '@/components/ui/Badge';
import { ErrorDiagnostic } from '@/components/ui/ErrorDiagnostic';
import { downloadBlob } from '@/lib/api';
import { formatBytes, formatMs } from '@/lib/format';
import type { EngineRunResult } from '@/lib/types';

const TRAITS: Record<EngineRunResult['engine'], string> = {
  gotenberg: 'Chromium — executes JavaScript',
  aspose: 'Aspose.Words — static HTML, no JS',
};

export function EngineCard({ result, sourceName }: { result: EngineRunResult; sourceName: string }) {
  return (
    <div
      data-testid={`engine-card-${result.engine}`}
      className="rounded-lg border border-surface-edge bg-surface p-4"
    >
      <div className="flex items-center justify-between">
        <EngineBadge engine={result.engine} />
        <span className="font-mono text-sm text-slate-300">{formatMs(result.latencyMs)}</span>
      </div>
      <p className="mt-1 text-xs text-slate-500">{TRAITS[result.engine]}</p>

      {result.ok && result.blob ? (
        <div className="mt-3 flex items-center justify-between">
          <span className="text-xs text-slate-400">{formatBytes(result.sizeBytes)}</span>
          <button
            type="button"
            className="btn-ghost"
            data-testid={`engine-download-${result.engine}`}
            onClick={() =>
              result.blob && downloadBlob(result.blob, `${sourceName}.${result.engine}.pdf`)
            }
          >
            ⬇ PDF
          </button>
        </div>
      ) : (
        <div className="mt-3">
          <ErrorDiagnostic error={result.error ?? 'failed'} />
        </div>
      )}
      {result.requestId && (
        <p className="mt-2 font-mono text-[10px] text-slate-600">req {result.requestId}</p>
      )}
    </div>
  );
}
