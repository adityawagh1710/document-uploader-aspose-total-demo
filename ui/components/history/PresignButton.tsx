'use client';

import { useState } from 'react';
import { ApiError, presign } from '@/lib/api';

// BR-UI-5: presigned URLs are minted fresh per click, never cached — a stored
// URL outlives its TTL and produces confusing 403s.
export function PresignButton({ s3Uri }: { s3Uri: string }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function mint() {
    const m = /^s3:\/\/([^/]+)\/(.+)$/.exec(s3Uri);
    if (!m || !m[1] || !m[2]) {
      setFailed(true);
      return;
    }
    setBusy(true);
    setFailed(false);
    try {
      const p = await presign(m[1], m[2]);
      window.open(p.download_url, '_blank', 'noopener');
    } catch (err) {
      setFailed(true);
      if (!(err instanceof ApiError)) throw err;
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      className="btn-ghost text-xs"
      data-testid="presign-button"
      disabled={busy}
      onClick={mint}
      title={s3Uri}
    >
      {busy ? '…' : failed ? 'retry S3 ⬇' : 'S3 ⬇'}
    </button>
  );
}
