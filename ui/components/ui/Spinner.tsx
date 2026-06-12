export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span role="status" aria-label={label} className="inline-flex items-center gap-2">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-surface-edge border-t-accent" />
      <span className="text-xs text-slate-400">{label}…</span>
    </span>
  );
}
