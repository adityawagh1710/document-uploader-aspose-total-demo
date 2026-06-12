import { Card } from '@/components/ui/Card';

// The ONLY direct browser→API URL in the app (BR-UI-1 exemption): the Go
// API's self-refreshing /v1/dashboard page, embedded as-is.
export function DashboardFrame({ src }: { src: string }) {
  return (
    <Card className="p-2">
      <iframe
        src={src}
        title="Live conversion dashboard"
        data-testid="dashboard-iframe"
        className="h-[640px] w-full rounded-lg border-0 bg-surface"
      />
    </Card>
  );
}
