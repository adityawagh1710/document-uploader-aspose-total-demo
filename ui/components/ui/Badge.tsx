import clsx from 'clsx';
import type { Engine } from '@/lib/types';

type Tone = 'ok' | 'err' | 'warn' | 'neutral' | 'gotenberg' | 'aspose';

const tones: Record<Tone, string> = {
  ok: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  err: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
  warn: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  neutral: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  gotenberg: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30',
  aspose: 'bg-violet-500/15 text-violet-300 border-violet-500/30',
};

export function Badge({
  tone = 'neutral',
  children,
  testId,
}: {
  tone?: Tone;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <span
      data-testid={testId}
      className={clsx(
        'inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-xs',
        tones[tone],
      )}
    >
      {children}
    </span>
  );
}

export function EngineBadge({ engine }: { engine: Engine }) {
  return (
    <Badge tone={engine} testId={`engine-badge-${engine}`}>
      ⚙ {engine}
    </Badge>
  );
}
