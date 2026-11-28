import { AlertTriangle, CopyPlus, RefreshCw } from 'lucide-react';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { OriginBadge } from '@/components/glass/OriginBadge';
import type { ConflictErrorDetail } from '@/types/contracts';

interface ConflictBannerProps {
  conflict: ConflictErrorDetail;
  onRefreshFromServer: () => void;
  onSaveAsSeparateDraft: () => void;
  savingFork?: boolean;
}

function formatDiffValue(val: unknown): string {
  if (val === null || val === undefined) return '—';
  if (typeof val === 'string') return val;
  try {
    return JSON.stringify(val, null, 2);
  } catch {
    return String(val);
  }
}

/**
 * Obsidian Glass 409 `stale_plan_version` conflict banner.
 * Never overwrites remote edits silently: displays diff + Refresh or Fork as Draft.
 */
export function ConflictBanner({
  conflict,
  onRefreshFromServer,
  onSaveAsSeparateDraft,
  savingFork = false,
}: ConflictBannerProps) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-2.5 rounded-lg border border-rose-500/40 bg-rose-500/[0.08] p-3 shadow-[0_8px_28px_rgba(0,0,0,0.45)] backdrop-blur-xl"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0 text-rose-400" strokeWidth={1.75} />
          <span className="text-xs font-semibold text-rose-200">
            Konflikt zapisu (409 {conflict.code})
          </span>
          <GlassBadge tone="rose">
            expected v{conflict.expected_version} != server v{conflict.current_version}
          </GlassBadge>
        </div>
        {conflict.modified_by && <OriginBadge origin={conflict.modified_by} compact />}
      </div>

      <p className="text-xs leading-relaxed text-zinc-200">{conflict.detail}</p>

      {conflict.diff.length > 0 && (
        <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-zinc-950/80 p-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 font-mono-tabular">
            Podgląd różnic (Twoja edycja vs stan w bazie)
          </span>
          <div className="max-h-40 overflow-y-auto space-y-1.5">
            {conflict.diff.map((row) => (
              <div
                key={row.field}
                className="rounded border border-white/[0.06] bg-white/[0.02] p-1.5 text-[11px] font-mono-tabular"
              >
                <div className="mb-1 flex items-center justify-between">
                  <span className="font-semibold text-sky-300">{row.field}</span>
                  <GlassBadge tone="amber">{row.change_type}</GlassBadge>
                </div>
                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                  <div className="rounded border border-amber-500/20 bg-amber-500/[0.05] p-1.5">
                    <div className="text-[10px] text-amber-400">Lokalnie (Twój draft)</div>
                    <pre className="mt-0.5 max-h-20 overflow-auto whitespace-pre-wrap break-words text-[10px] text-zinc-200">
                      {formatDiffValue(row.before)}
                    </pre>
                  </div>
                  <div className="rounded border border-emerald-500/20 bg-emerald-500/[0.05] p-1.5">
                    <div className="text-[10px] text-emerald-400">W bazie (v{conflict.current_version})</div>
                    <pre className="mt-0.5 max-h-20 overflow-auto whitespace-pre-wrap break-words text-[10px] text-zinc-200">
                      {formatDiffValue(row.after)}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2 pt-0.5">
        <button
          type="button"
          onClick={onRefreshFromServer}
          className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.14] bg-white/[0.05] px-2.5 text-xs font-medium text-zinc-100 transition-colors hover:bg-white/[0.1]"
        >
          <RefreshCw className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
          <span>Odśwież stan z bazy</span>
        </button>
        <button
          type="button"
          disabled={savingFork}
          onClick={onSaveAsSeparateDraft}
          className="flex h-7 items-center gap-1.5 rounded-md border border-amber-500/35 bg-amber-500/15 px-2.5 text-xs font-medium text-amber-200 transition-colors hover:bg-amber-500/25 disabled:opacity-50"
        >
          <CopyPlus className="h-3.5 w-3.5" strokeWidth={1.5} />
          <span>{savingFork ? 'Zapisywanie…' : 'Zapisz jako osobny draft'}</span>
        </button>
      </div>
    </div>
  );
}
