import { ConvertPanel } from '@/components/convert/ConvertPanel';
import { ComparePanel } from '@/components/html-compare/ComparePanel';
import { HistoryPanel } from '@/components/history/HistoryPanel';
import { HealthTiles } from '@/components/stats/HealthTiles';
import { PerfPanel } from '@/components/stats/PerfPanel';
import { DashboardFrame } from '@/components/dashboard/DashboardFrame';

// Server component shell: runtime env is read HERE (not inlined at build) and
// passed down as props, so compose env vars take effect without a rebuild.
// force-dynamic keeps the env reads at request time — a static prerender
// would bake build-machine values into the HTML.
export const dynamic = 'force-dynamic';

export default function Page() {
  const dashboardUrl =
    process.env.NEXT_PUBLIC_DASHBOARD_URL ?? 'http://localhost:8080/v1/dashboard';
  const s3Enabled = (process.env.NEXT_PUBLIC_S3_ENABLED ?? 'false') === 'true';
  const s3OutputBucket = process.env.NEXT_PUBLIC_S3_OUTPUT_BUCKET ?? 'office-convert-out';

  return (
    <div>
      <section aria-labelledby="stats-heading" className="section-gap">
        <h2 id="stats-heading" className="sr-only">
          Service health
        </h2>
        <HealthTiles />
      </section>

      <div className="section-gap grid gap-8 lg:grid-cols-2">
        <section aria-labelledby="convert-heading">
          <h2 id="convert-heading" className="mb-4 text-base font-semibold text-slate-200">
            Convert a document
          </h2>
          <ConvertPanel s3Enabled={s3Enabled} s3OutputBucket={s3OutputBucket} />
        </section>

        <section aria-labelledby="compare-heading">
          <h2 id="compare-heading" className="mb-4 text-base font-semibold text-slate-200">
            HTML → PDF · engine comparison
          </h2>
          <ComparePanel />
        </section>
      </div>

      <section aria-labelledby="history-heading" className="section-gap">
        <h2 id="history-heading" className="mb-4 text-base font-semibold text-slate-200">
          Conversion history
        </h2>
        <HistoryPanel s3Enabled={s3Enabled} />
      </section>

      <section aria-labelledby="perf-heading" className="section-gap">
        <h2 id="perf-heading" className="mb-4 text-base font-semibold text-slate-200">
          Performance
        </h2>
        <PerfPanel />
      </section>

      <section aria-labelledby="dashboard-heading" className="section-gap">
        <h2 id="dashboard-heading" className="mb-4 text-base font-semibold text-slate-200">
          Live dashboard
        </h2>
        <DashboardFrame src={dashboardUrl} />
      </section>
    </div>
  );
}
