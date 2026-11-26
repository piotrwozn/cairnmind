import { Code2, ShieldAlert } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { formatTimeMs } from '@/lib/utils';
import type { GuardrailAuditEntry } from '@/types/contracts';

interface McpPayloadInspectorProps {
  entry: GuardrailAuditEntry;
}

export function McpPayloadInspector({ entry }: McpPayloadInspectorProps) {
  const isRejected = entry.verdict === 'REJECTED';

  return (
    <div className="flex flex-col gap-3">
      {/* Header metadata */}
      <div className="flex flex-col gap-2 rounded-md border border-white/[0.08] bg-zinc-950/60 p-2.5 text-xs">
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono-tabular font-semibold text-sky-300">{entry.mcp_tool}</span>
          <GlassBadge tone={statusToTone(entry.verdict)}>{entry.verdict}</GlassBadge>
        </div>

        <div className="grid grid-cols-2 gap-1.5 text-[11px] font-mono-tabular">
          <div>
            <span className="text-zinc-500">time: </span>
            <span className="text-zinc-300">{formatTimeMs(entry.timestamp)}</span>
          </div>
          <div>
            <span className="text-zinc-500">client: </span>
            <span className="text-zinc-300">{entry.client}</span>
          </div>
          <div className="col-span-2 truncate">
            <span className="text-zinc-500">session: </span>
            <span className="text-zinc-200">{entry.session_id}</span>
          </div>
          <div className="col-span-2">
            <span className="text-zinc-500">rule: </span>
            <span className={isRejected ? 'text-rose-400' : 'text-emerald-400'}>
              {entry.reason_code}
            </span>
          </div>
        </div>

        <p className="border-t border-white/[0.06] pt-2 text-xs text-zinc-300">{entry.message}</p>
      </div>

      {/* Side-by-side / Split JSON Diff Inspector */}
      <div className="grid grid-cols-1 gap-2.5">
        <div className="flex flex-col overflow-hidden rounded-md border border-white/[0.08] bg-zinc-950/75">
          <div className="flex items-center gap-1.5 border-b border-white/[0.06] bg-white/[0.02] px-2.5 py-1.5 text-[11px] font-medium text-zinc-300">
            <Code2 className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
            <span>LLM MCP Arguments</span>
          </div>
          <pre className="max-h-[220px] overflow-auto whitespace-pre-wrap break-words p-2.5 text-[11px] leading-relaxed text-zinc-200 font-mono-tabular">
            {JSON.stringify(entry.arguments, null, 2)}
          </pre>
        </div>

        <div
          className={
            isRejected
              ? 'flex flex-col overflow-hidden rounded-md border border-rose-500/25 bg-rose-500/[0.05]'
              : 'flex flex-col overflow-hidden rounded-md border border-emerald-500/25 bg-emerald-500/[0.05]'
          }
        >
          <div className="flex items-center gap-1.5 border-b border-white/[0.06] bg-white/[0.02] px-2.5 py-1.5 text-[11px] font-medium text-zinc-200">
            <ShieldAlert
              className={isRejected ? 'h-3.5 w-3.5 text-rose-400' : 'h-3.5 w-3.5 text-emerald-400'}
              strokeWidth={1.5}
            />
            <span>{isRejected ? 'Contract Violation Detail' : 'Verification Verdict Detail'}</span>
          </div>
          <pre
            className={
              isRejected
                ? 'max-h-[220px] overflow-auto whitespace-pre-wrap break-words p-2.5 text-[11px] leading-relaxed text-rose-200 font-mono-tabular'
                : 'max-h-[220px] overflow-auto whitespace-pre-wrap break-words p-2.5 text-[11px] leading-relaxed text-emerald-200 font-mono-tabular'
            }
          >
            {JSON.stringify(entry.violation_detail, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  );
}
