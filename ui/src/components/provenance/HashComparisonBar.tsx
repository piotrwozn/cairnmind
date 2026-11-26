import { AlertTriangle, CheckCircle2, FileX2, Hash, ShieldAlert } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { shortHash } from '@/lib/utils';
import type { DriftKind } from '@/types/contracts';

interface HashComparisonBarProps {
  recordedSha256: string;
  currentDiskSha256: string;
  driftKind: DriftKind;
}

function driftSummary(driftKind: DriftKind): { text: string; icon: typeof CheckCircle2 } {
  switch (driftKind) {
    case 'MATCH':
      return { text: 'File SHA-256 matches recorded snapshot; verbatim quote verified.', icon: CheckCircle2 };
    case 'FILE_DRIFT_QUOTE_INTACT':
      return {
        text: 'File hash drifted since capture, but the exact verbatim quote still exists on disk.',
        icon: AlertTriangle,
      };
    case 'QUOTE_MUTATED':
      return {
        text: 'File hash drifted AND the verbatim quote was mutated or deleted on disk.',
        icon: ShieldAlert,
      };
    case 'FILE_MISSING':
      return { text: 'Cited source file no longer exists on disk.', icon: FileX2 };
  }
}

export function HashComparisonBar({
  recordedSha256,
  currentDiskSha256,
  driftKind,
}: HashComparisonBarProps) {
  const { text, icon: Icon } = driftSummary(driftKind);
  const matched = Boolean(recordedSha256 && currentDiskSha256 && recordedSha256 === currentDiskSha256);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-white/[0.08] bg-zinc-950/60 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
          <Hash className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
          <span>SHA-256 Fingerprint Verification</span>
        </div>
        <GlassBadge tone={statusToTone(driftKind)}>{driftKind}</GlassBadge>
      </div>

      <div className="grid grid-cols-2 gap-2 text-[11px] font-mono-tabular">
        <div className="rounded border border-white/[0.06] bg-white/[0.02] px-2 py-1.5">
          <div className="text-[10px] text-zinc-500">recorded_sha256</div>
          <div className="truncate text-zinc-200" title={recordedSha256}>
            {shortHash(recordedSha256, 16)}
          </div>
        </div>
        <div className="rounded border border-white/[0.06] bg-white/[0.02] px-2 py-1.5">
          <div className="text-[10px] text-zinc-500">current_disk_sha256</div>
          <div
            className={matched ? 'truncate text-emerald-400' : 'truncate text-amber-400'}
            title={currentDiskSha256}
          >
            {shortHash(currentDiskSha256, 16)}
          </div>
        </div>
      </div>

      <div className="flex items-start gap-1.5 text-[11px] text-zinc-400">
        <Icon
          className={
            driftKind === 'MATCH'
              ? 'mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400'
              : driftKind === 'FILE_DRIFT_QUOTE_INTACT'
                ? 'mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400'
                : 'mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-400'
          }
          strokeWidth={1.5}
        />
        <span>{text}</span>
      </div>
    </div>
  );
}
