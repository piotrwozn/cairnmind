import { useQuery } from '@tanstack/react-query';
import { AlertCircle, FileCode2, Loader2 } from 'lucide-react';
import { fetchFileContext } from '@/api/client';
import { HashComparisonBar } from '@/components/provenance/HashComparisonBar';
import { cn } from '@/lib/utils';
import type { EvidenceClaim } from '@/types/contracts';

interface VerbatimCodeViewerProps {
  claim: EvidenceClaim;
}

export function VerbatimCodeViewer({ claim }: VerbatimCodeViewerProps) {
  const lineStart = Math.max(1, claim.line_start - 4);
  const lineEnd = Math.max(claim.line_end + 4, lineStart + 18);

  const { data, isLoading, error } = useQuery({
    queryKey: ['ui-file-context', claim.source_path, lineStart, lineEnd],
    queryFn: () => fetchFileContext(claim.source_path, lineStart, lineEnd),
    enabled: Boolean(claim.source_path),
    staleTime: 10_000,
  });

  // Locate exact contiguous verbatim quote span inside the loaded snippet
  const highlightedRange: [number, number] | null = (() => {
    if (!claim.verbatim_quote || !data?.lines.length) return null;
    const joined = data.lines.map((l) => l.text).join('\n');
    const normalizedQuote = claim.verbatim_quote.replace(/\r\n/g, '\n');
    const idx = joined.indexOf(normalizedQuote);
    if (idx >= 0) {
      const startOffset = joined.slice(0, idx).split('\n').length - 1;
      const lineSpan = normalizedQuote.split('\n').length;
      const firstLine = data.lines[startOffset]?.number ?? claim.line_start;
      return [firstLine, firstLine + lineSpan - 1];
    }
    if (claim.drift_kind === 'QUOTE_MUTATED') {
      return [claim.line_start, claim.line_end];
    }
    return null;
  })();

  const lineMatchesQuote = (lineNo: number): boolean => {
    if (!highlightedRange) return false;
    return lineNo >= highlightedRange[0] && lineNo <= highlightedRange[1];
  };

  return (
    <div className="flex flex-col gap-3">
      <HashComparisonBar
        recordedSha256={claim.recorded_sha256}
        currentDiskSha256={data?.current_disk_sha256 ?? claim.current_disk_sha256}
        driftKind={claim.drift_kind}
      />

      {/* Verbatim Quote Box */}
      <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-zinc-950/70 p-2.5">
        <div className="flex items-center justify-between text-[11px] font-mono-tabular">
          <span className="text-zinc-400">Stored Verbatim Quote</span>
          <span className="text-zinc-500">
            L{claim.line_start}–L{claim.line_end}
          </span>
        </div>
        <pre
          className={cn(
            'overflow-x-auto rounded border p-2 text-[11px] leading-relaxed font-mono-tabular whitespace-pre-wrap',
            claim.drift_kind === 'MATCH' && 'border-emerald-500/25 bg-emerald-500/[0.07] text-emerald-200',
            claim.drift_kind === 'FILE_DRIFT_QUOTE_INTACT' &&
              'border-amber-500/25 bg-amber-500/[0.07] text-amber-200',
            (claim.drift_kind === 'QUOTE_MUTATED' || claim.drift_kind === 'FILE_MISSING') &&
              'border-rose-500/25 bg-rose-500/[0.07] text-rose-200',
          )}
        >
          {claim.verbatim_quote || '(No verbatim quote stored on this claim)'}
        </pre>
      </div>

      {/* Live Disk Code Snippet */}
      <div className="flex flex-col overflow-hidden rounded-md border border-white/[0.08] bg-zinc-950/80 backdrop-blur-xl">
        <div className="flex items-center justify-between border-b border-white/[0.06] bg-white/[0.02] px-2.5 py-1.5 text-[11px] font-mono-tabular">
          <span className="flex min-w-0 items-center gap-1.5 text-zinc-300">
            <FileCode2 className="h-3.5 w-3.5 shrink-0 text-sky-400" strokeWidth={1.5} />
            <span className="truncate">{claim.source_path || 'No file path'}</span>
          </span>
          {data && (
            <span className="shrink-0 text-zinc-500">
              L{data.line_start}–{data.line_end} / {data.total_lines}
            </span>
          )}
        </div>

        {!claim.source_path ? (
          <div className="p-3 text-xs text-zinc-500 font-mono-tabular">
            This claim is not backed by a repository file path.
          </div>
        ) : isLoading ? (
          <div className="flex items-center justify-center gap-2 p-6 text-xs text-zinc-400 font-mono-tabular">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-400" strokeWidth={1.5} />
            <span>Loading disk snippet...</span>
          </div>
        ) : error ? (
          <div className="flex items-center gap-2 p-3 text-xs text-rose-400 font-mono-tabular">
            <AlertCircle className="h-4 w-4 shrink-0" strokeWidth={1.5} />
            <span>{error instanceof Error ? error.message : 'Unable to read file context'}</span>
          </div>
        ) : (
          <div className="max-h-[320px] overflow-auto py-1 font-mono-tabular text-[11px] leading-5">
            {data?.lines.map((line) => {
              const highlighted = lineMatchesQuote(line.number);
              return (
                <div
                  key={line.number}
                  className={cn(
                    'flex px-2',
                    highlighted &&
                      claim.drift_kind === 'MATCH' &&
                      'border-l-2 border-emerald-400 bg-emerald-500/[0.12]',
                    highlighted &&
                      claim.drift_kind === 'FILE_DRIFT_QUOTE_INTACT' &&
                      'border-l-2 border-amber-400 bg-amber-500/[0.12]',
                    highlighted &&
                      claim.drift_kind === 'QUOTE_MUTATED' &&
                      'border-l-2 border-rose-400 bg-rose-500/[0.12]',
                    !highlighted && 'border-l-2 border-transparent',
                  )}
                >
                  <span className="w-9 shrink-0 select-none pr-2 text-right text-zinc-600">
                    {line.number}
                  </span>
                  <span
                    className={cn(
                      'whitespace-pre',
                      highlighted ? 'text-zinc-100' : 'text-zinc-400',
                    )}
                  >
                    {line.text || ' '}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
