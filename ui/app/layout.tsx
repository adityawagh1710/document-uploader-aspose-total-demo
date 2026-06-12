import type { Metadata } from 'next';
import './globals.css';
import { HealthPill } from '@/components/stats/HealthPill';

export const metadata: Metadata = {
  title: 'office-convert · operator dashboard',
  description: 'Office → PDF conversion service dashboard',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <header className="sticky top-0 z-10 border-b border-surface-edge bg-surface/90 backdrop-blur">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
            <div className="flex items-baseline gap-3">
              <h1 className="text-lg font-bold tracking-tight">
                office<span className="text-accent">-</span>convert
              </h1>
              <span className="text-xs text-slate-500">operator dashboard</span>
            </div>
            <HealthPill />
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
        <footer className="mx-auto max-w-6xl px-6 pb-8 pt-4 text-xs text-slate-600">
          Go orchestrator · C++ Aspose workers · Gotenberg (Chromium)
        </footer>
      </body>
    </html>
  );
}
