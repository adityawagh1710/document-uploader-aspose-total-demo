import clsx from 'clsx';

export function Card({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={clsx(
        'animate-fade-in-up rounded-xl border border-surface-edge bg-surface-raised p-5',
        'shadow-lg shadow-black/20 transition-[transform,border-color,box-shadow] duration-200',
        // Subtle lift + cyan edge-glow on hover — the surface feels touchable
        // without pulling focus from the data.
        'hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-[0_10px_30px_-12px_rgba(34,211,238,0.35)]',
        className,
      )}
    >
      {children}
    </div>
  );
}
