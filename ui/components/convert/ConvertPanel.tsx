'use client';

import { useRef, useState } from 'react';
import { Card } from '@/components/ui/Card';
import { Spinner } from '@/components/ui/Spinner';
import { ErrorDiagnostic } from '@/components/ui/ErrorDiagnostic';
import { ApiError, convertFile, downloadBlob } from '@/lib/api';
import { formatBytes, formatMs } from '@/lib/format';
import type { Diagnostic } from '@/lib/types';

const ACCEPT =
  '.docx,.doc,.dot,.pptx,.ppt,.pot,.pps,.xlsx,.xls,.xlt,.xlm,.csv,.pdf,.rtf,' +
  '.odt,.ods,.odp,.odg,.png,.jpg,.jpeg,.tiff,.tif,.gif,.bmp,.webp,.svg,.eml,.msg';

interface DoneState {
  filename: string;
  blob: Blob;
  latencyMs: number;
  requestId: string;
  s3Bucket: string | null;
  s3Key: string | null;
}

export function ConvertPanel({ s3Enabled }: { s3Enabled: boolean }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [s3Output, setS3Output] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<DoneState | null>(null);
  const [error, setError] = useState<Diagnostic | string | null>(null);

  async function submit() {
    if (!file) return;
    setBusy(true);
    setError(null);
    setDone(null);
    const started = performance.now();
    try {
      const res = await convertFile(file, s3Output ? { s3Output: 'office-convert-out' } : {});
      setDone({
        filename: file.name.replace(/\.[^.]+$/, '') + '.pdf',
        blob: res.blob,
        latencyMs: performance.now() - started,
        requestId: res.requestId,
        s3Bucket: res.s3Bucket,
        s3Key: res.s3Key,
      });
    } catch (err) {
      if (err instanceof ApiError) setError(err.diagnostic ?? err.message);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-col gap-4">
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT}
          data-testid="convert-file-input"
          aria-label="Document to convert"
          className="text-sm text-slate-400 file:mr-3 file:rounded-lg file:border-0 file:bg-surface-edge file:px-3 file:py-1.5 file:text-sm file:text-slate-200 hover:file:bg-slate-700"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setDone(null);
            setError(null);
          }}
        />
        {file && (
          <p className="text-xs text-slate-500">
            {file.name} · {formatBytes(file.size)}
          </p>
        )}

        {s3Enabled && (
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={s3Output}
              data-testid="convert-s3-checkbox"
              onChange={(e) => setS3Output(e.target.checked)}
              className="accent-cyan-400"
            />
            Also store output in S3 (office-convert-out)
          </label>
        )}

        <div>
          <button
            type="button"
            className="btn-primary"
            data-testid="convert-submit-button"
            disabled={!file || busy}
            onClick={submit}
          >
            {busy ? 'Converting…' : 'Convert to PDF'}
          </button>
        </div>

        {busy && <Spinner label="Converting" />}
        {error && <ErrorDiagnostic error={error} />}
        {done && (
          <div
            data-testid="convert-result"
            className="rounded-lg bg-emerald-500/10 p-3 text-sm text-emerald-300"
          >
            <p>
              Done in {formatMs(done.latencyMs)} · {formatBytes(done.blob.size)}
              <span className="ml-2 font-mono text-xs text-slate-500">req {done.requestId}</span>
            </p>
            <div className="mt-2 flex items-center gap-3">
              <button
                type="button"
                className="btn-ghost"
                data-testid="convert-download-button"
                onClick={() => downloadBlob(done.blob, done.filename)}
              >
                ⬇ Download {done.filename}
              </button>
              {done.s3Bucket && done.s3Key && (
                <span className="font-mono text-xs text-slate-500">
                  s3://{done.s3Bucket}/{done.s3Key}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
