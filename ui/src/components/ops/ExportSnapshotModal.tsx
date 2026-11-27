import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Check,
  CheckCircle2,
  ClipboardCopy,
  Download,
  Loader2,
  ShieldCheck,
  X,
} from 'lucide-react';
import { exportTeamSnapshot } from '@/api/client';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { shortHash } from '@/lib/utils';
import type { ExportSnapshotResponse } from '@/types/contracts';

interface ExportSnapshotModalProps {
  open: boolean;
  writable: boolean;
  readOnlyReason?: string | null;
  onClose: () => void;
}

export function ExportSnapshotModal({
  open,
  writable,
  readOnlyReason,
  onClose,
}: ExportSnapshotModalProps) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ExportSnapshotResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const runExport = async (preview: boolean) => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await exportTeamSnapshot(preview);
      setResult(res);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) {
      // Always start with a safe dry-run preview so redaction gate runs immediately even in read-only mode.
      void runExport(true);
    }
  }, [open]);

  const handleCopyJson = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(result.payload_preview, null, 2));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // ignore clipboard restrictions
    }
  };

  const handleDownload = () => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result.payload_preview, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = result.artifact_filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -6 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            className="flex max-h-[85vh] w-full max-w-[660px] flex-col overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/90 shadow-[0_20px_60px_rgba(0,0,0,0.75),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.08] px-4">
              <div className="flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-emerald-400" strokeWidth={1.5} />
                <span className="text-xs font-semibold text-zinc-100">
                  Export Safe Snapshot (team)
                </span>
                {result && (
                  <GlassBadge
                    tone={
                      result.redaction_gate.status === 'PASS'
                        ? 'emerald'
                        : result.redaction_gate.status === 'REDACTED'
                          ? 'amber'
                          : 'rose'
                    }
                  >
                    REDACTION GATE: {result.redaction_gate.status}
                  </GlassBadge>
                )}
              </div>
              <button
                type="button"
                onClick={onClose}
                className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100"
              >
                <X className="h-4 w-4" strokeWidth={1.5} />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-4 space-y-3">
              {errorMsg && (
                <div className="rounded border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-200">
                  {errorMsg}
                </div>
              )}

              {loading && !result ? (
                <div className="flex h-44 items-center justify-center gap-2 text-xs text-zinc-400 font-mono-tabular">
                  <Loader2 className="h-4 w-4 animate-spin text-sky-400" strokeWidth={1.5} />
                  <span>Running redaction gate &amp; assembling safe team snapshot…</span>
                </div>
              ) : result ? (
                <>
                  {/* Redaction Gate + Summary */}
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-5 text-center font-mono-tabular">
                    <div className="rounded border border-emerald-500/20 bg-emerald-500/[0.06] p-2">
                      <div className="text-base font-semibold text-emerald-300">
                        {result.summary.confirmed_records}
                      </div>
                      <div className="text-[10px] text-zinc-400">Confirmed</div>
                    </div>
                    <div className="rounded border border-sky-500/20 bg-sky-500/[0.06] p-2">
                      <div className="text-base font-semibold text-sky-300">
                        {result.summary.plans_included}
                      </div>
                      <div className="text-[10px] text-zinc-400">Plans</div>
                    </div>
                    <div className="rounded border border-white/[0.08] bg-white/[0.02] p-2">
                      <div className="text-base font-semibold text-zinc-300">
                        {result.summary.held_proposed}
                      </div>
                      <div className="text-[10px] text-zinc-500">Held Proposed</div>
                    </div>
                    <div className="rounded border border-amber-500/20 bg-amber-500/[0.06] p-2">
                      <div className="text-base font-semibold text-amber-300">
                        {result.summary.held_degraded}
                      </div>
                      <div className="text-[10px] text-zinc-400">Held Degraded</div>
                    </div>
                    <div className="rounded border border-white/[0.08] bg-white/[0.02] p-2">
                      <div className="text-base font-semibold text-zinc-300">
                        {result.redaction_gate.secrets_redacted}
                      </div>
                      <div className="text-[10px] text-zinc-500">Redacted</div>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-emerald-500/20 bg-emerald-500/[0.05] px-3 py-2 text-[11px] font-mono-tabular text-emerald-200">
                    <span className="flex items-center gap-1.5">
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
                      <span>
                        Scanned {result.redaction_gate.secrets_scanned} secret rules ·{' '}
                        {result.summary.excluded_transcripts} private transcripts excluded
                      </span>
                    </span>
                    <span className="text-zinc-400">sha256:{shortHash(result.snapshot_sha256, 12)}</span>
                  </div>

                  <div className="flex flex-col overflow-hidden rounded-md border border-white/[0.08] bg-zinc-950/80">
                    <div className="flex items-center justify-between border-b border-white/[0.06] bg-white/[0.02] px-3 py-1.5 text-[11px] font-mono-tabular text-zinc-400">
                      <span>{result.artifact_filename}</span>
                      <span>{result.preview ? 'DRY-RUN PREVIEW' : 'EXPORTED'}</span>
                    </div>
                    <pre className="max-h-60 overflow-auto p-3 text-[11px] leading-relaxed text-zinc-300 font-mono-tabular">
                      {JSON.stringify(result.payload_preview, null, 2)}
                    </pre>
                  </div>
                </>
              ) : null}
            </div>

            <div className="flex h-12 shrink-0 items-center justify-between border-t border-white/[0.08] bg-zinc-950/60 px-4">
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  disabled={!result}
                  onClick={() => void handleCopyJson()}
                  className="flex h-7 items-center gap-1.5 rounded border border-white/[0.1] bg-white/[0.04] px-2.5 text-xs text-zinc-200 hover:bg-white/[0.08] disabled:opacity-40"
                >
                  {copied ? (
                    <Check className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
                  ) : (
                    <ClipboardCopy className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                  )}
                  <span>{copied ? 'Copied JSON' : 'Copy JSON'}</span>
                </button>
                <button
                  type="button"
                  disabled={!result}
                  onClick={handleDownload}
                  className="flex h-7 items-center gap-1.5 rounded border border-white/[0.1] bg-white/[0.04] px-2.5 text-xs text-zinc-200 hover:bg-white/[0.08] disabled:opacity-40"
                >
                  <Download className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                  <span>Download .json</span>
                </button>
              </div>

              <button
                type="button"
                disabled={!writable || loading}
                title={writable ? 'Finalize team export snapshot' : readOnlyReason || 'Read-only mode: preview & download only'}
                onClick={() => void runExport(false)}
                className="flex h-7 items-center gap-1.5 rounded-md border border-emerald-500/35 bg-emerald-500/15 px-3 text-xs font-medium text-emerald-200 transition-colors hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                <span>Commit Safe Team Export</span>
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
