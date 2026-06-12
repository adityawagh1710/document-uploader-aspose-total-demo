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
        'rounded-xl border border-surface-edge bg-surface-raised p-5 shadow-lg shadow-black/20',
        className,
      )}
    >
      {children}
    </div>
  );
}
