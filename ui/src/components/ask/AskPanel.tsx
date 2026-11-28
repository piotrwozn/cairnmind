import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  BookmarkPlus,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  ExternalLink,
  FileCode2,
  HelpCircle,
  History,
  Loader2,
  Send,
  Sparkles,
  Square,
  Trash2,
  WifiOff,
} from 'lucide-react';
import { askCockpit, getCockpitAdapter } from '@/api/client';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { OriginBadge } from '@/components/glass/OriginBadge';
import { cn, formatShortTime, shortHash } from '@/lib/utils';
import type {
  AskCitation,
  AskResponse,
  AskVerifierMode,
  UiSnapshot,
} from '@/types/contracts';

const ONBOARDING_PROMPTS: Array<{
  title: string;
  query: string;
  tag: string;
}> = [
  {
    title: 'Architektura i przepływ danych',
    query: 'Jakie są główne komponenty i przepływ danych?',
    tag: 'Onboarding',
  },
  {
    title: 'Ostatnie decyzje architektoniczne',
    query: 'Jakie decyzje architektoniczne wykryto ostatnio w projekcie?',
    tag: 'Decisions',
  },
  {
    title: 'Nieaktualne zadania i dowody',
    query: 'Które zadania lub dowody są teraz nieaktualne (stale)?',
    tag: 'Staleness',
  },
  {
    title: 'Test twardego UNKNOWN (bez halucynacji)',
    query: 'Jak działa integracja z płatnościami Stripe i klastrem Kubernetes?',
    tag: 'Zero-Hallucination',
  },
];

interface AskPanelProps {
  snapshot: UiSnapshot | undefined;
  initialQuery?: string | null;
  onConsumeInitialQuery?: () => void;
  changedFingerprints: Readonly<Record<string, string>>;
  liveStaleClaimIds: ReadonlySet<string>;
  onSelectCitation: (citation: AskCitation) => void;
  onPinAsFact: (answer: AskResponse) => void;
  compact?: boolean;
}

function storageKeyFor(projectId: string | undefined): string {
  return `cairnmind:ask-history:${projectId ?? 'default'}`;
}

export function AskPanel({
  snapshot,
  initialQuery,
  onConsumeInitialQuery,
  changedFingerprints,
  liveStaleClaimIds,
  onSelectCitation,
  onPinAsFact,
  compact = false,
}: AskPanelProps) {
  const storageKey = storageKeyFor(snapshot?.project.id);
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState('');
  const [verifierMode, setVerifierMode] = useState<AskVerifierMode>('auto');
  const [history, setHistory] = useState<AskResponse[]>(() => {
    try {
      const raw = window.sessionStorage.getItem(storageKey);
      return raw ? (JSON.parse(raw) as AskResponse[]) : [];
    } catch {
      return [];
    }
  });
  const [activeAnswerId, setActiveAnswerId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [expandedCitations, setExpandedCitations] = useState<Record<string, boolean>>({});
  const abortRef = useRef<AbortController | null>(null);

  const writable = snapshot?.capabilities?.writable ?? true;
  const readOnlyReason = snapshot?.capabilities?.read_only_reason;
  const llmConfigured = snapshot?.capabilities?.llm_configured ?? true;
  const adapter = getCockpitAdapter();
  const isMock = adapter.mode === 'mock';

  useEffect(() => {
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(history.slice(0, 20)));
    } catch {
      // ignore storage quota errors
    }
  }, [history, storageKey]);

  const activeAnswer = useMemo(() => {
    if (activeAnswerId) {
      return history.find((item) => item.id === activeAnswerId) ?? history[0] ?? null;
    }
    return history[0] ?? null;
  }, [activeAnswerId, history]);

  const runAsk = useCallback(
    async (rawQuestion: string, overrideMode?: AskVerifierMode) => {
      const trimmed = rawQuestion.trim();
      if (!trimmed) return;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setLoading(true);
      setErrorMsg(null);

      try {
        const res = await askCockpit(
          {
            query: trimmed,
            scope: scope.trim() || null,
            verifier_mode: overrideMode ?? verifierMode,
          },
          controller.signal,
        );
        setHistory((prev) => [res, ...prev].slice(0, 20));
        setActiveAnswerId(res.id);
        setExpandedCitations((prev) => ({ ...prev, [res.id]: true }));
        setQuery('');
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          setErrorMsg('Zapytanie zostało anulowane (AbortController).');
        } else {
          // Graceful offline / Tier 0 FTS fallback hint instead of raw 500
          setErrorMsg(
            err instanceof Error
              ? `Nie udało się połączyć z modelem (${err.message}). Spróbuj trybu Poziom 0 / FTS.`
              : 'LLM niedostępny — przełącz na Poziom 0 / FTS.',
          );
        }
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
          setLoading(false);
        }
      }
    },
    [scope, verifierMode],
  );

  useEffect(() => {
    if (initialQuery && initialQuery.trim()) {
      setQuery(initialQuery);
      void runAsk(initialQuery);
      onConsumeInitialQuery?.();
    }
  }, [initialQuery, onConsumeInitialQuery, runAsk]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const handleCancel = () => {
    abortRef.current?.abort();
  };

  const isCitationStaleNow = (citation: AskCitation): boolean => {
    if (citation.stale_now) return true;
    const latestFp = changedFingerprints[citation.file_path];
    if (latestFp && citation.fingerprint && latestFp !== citation.fingerprint) {
      return true;
    }
    if (citation.claim_id && liveStaleClaimIds.has(citation.claim_id)) {
      return true;
    }
    return false;
  };

  const statusTone = (status: AskResponse['status']) => {
    if (status === 'VERIFIED') return 'emerald';
    if (status === 'PARTIAL') return 'amber';
    return 'rose';
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold text-zinc-100">Interactive Ask &amp; Onboarding</span>
          <GlassBadge tone={llmConfigured ? 'emerald' : 'amber'}>
            {llmConfigured ? 'VERIFIED EVIDENCE' : 'TIER 0 · FTS ONLY'}
          </GlassBadge>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {isMock && adapter.mockControls && (
            <>
              <button
                type="button"
                onClick={() => {
                  const flags = adapter.mockControls!.getMockFlags();
                  adapter.mockControls!.setLlmOffline(!flags.llmOffline);
                }}
                title="Toggle simulated offline / no-LLM Tier 0 FTS fallback mode"
                className={cn(
                  'flex h-6 items-center gap-1 rounded border px-2 text-[10px] font-medium font-mono-tabular transition-colors',
                  !llmConfigured
                    ? 'border-amber-500/40 bg-amber-500/15 text-amber-300'
                    : 'border-white/[0.08] bg-white/[0.03] text-zinc-400 hover:text-zinc-200',
                )}
              >
                <WifiOff className="h-3 w-3" strokeWidth={1.5} />
                <span>{llmConfigured ? 'Sim Offline LLM' : 'LLM Offline (ON)'}</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  const firstPath = activeAnswer?.citations[0]?.file_path || 'src/cairnmind/sessions.py';
                  void adapter.mockControls!.simulateFileEdit(firstPath);
                }}
                title="Simulate a live IDE edit on the cited file to trigger STALE NOW via SSE"
                className="flex h-6 items-center gap-1 rounded border border-amber-500/25 bg-amber-500/[0.08] px-2 text-[10px] font-medium text-amber-300 font-mono-tabular transition-colors hover:bg-amber-500/15"
              >
                <AlertTriangle className="h-3 w-3" strokeWidth={1.5} />
                <span>Sim File Edit (STALE NOW)</span>
              </button>
            </>
          )}

          <select
            aria-label="Ask verifier mode"
            value={verifierMode}
            onChange={(e) => setVerifierMode(e.target.value as AskVerifierMode)}
            className="h-6 rounded border border-white/[0.08] bg-zinc-950/80 px-1.5 text-[11px] text-zinc-300 font-mono-tabular focus:border-sky-500/40 focus:outline-none"
          >
            <option value="auto">mode: auto</option>
            <option value="strict">mode: strict</option>
            <option value="fts_fallback">mode: tier-0 FTS</option>
          </select>
        </div>
      </div>

      {/* Offline / No-LLM Notice */}
      {(!llmConfigured || verifierMode === 'fts_fallback') && (
        <div className="flex items-center justify-between gap-2 border-b border-amber-500/20 bg-amber-500/[0.06] px-3 py-1.5 text-[11px] text-amber-200">
          <div className="flex items-center gap-2">
            <WifiOff className="h-3.5 w-3.5 shrink-0 text-amber-400" strokeWidth={1.5} />
            <span>
              Brak skonfigurowanego LLM / tryb offline — zapytania działają w trybie czystych dowodów i cytatów (Poziom 0 / FTS).
            </span>
          </div>
        </div>
      )}

      {/* Main Content Area */}
      <div className={cn('flex min-h-0 flex-1 overflow-hidden', !compact && 'divide-x divide-white/[0.06]')}>
        {/* Left/Main Q&A Column */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {/* Query Input Form */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void runAsk(query);
            }}
            className="flex flex-col gap-2 border-b border-white/[0.06] bg-white/[0.015] p-3"
          >
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Zapytaj o architekturę, decyzje, blokery lub kod (odpowiedź wyłącznie z dowodów lub UNKNOWN)…"
                className="h-8 min-w-0 flex-1 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 placeholder:text-zinc-500 focus:border-sky-500/50 focus:outline-none"
              />
              {!compact && (
                <input
                  type="text"
                  value={scope}
                  onChange={(e) => setScope(e.target.value)}
                  placeholder="scope (opcjonalnie, np. src/)"
                  className="hidden h-8 w-44 rounded-md border border-white/[0.08] bg-zinc-950/70 px-2 text-xs text-zinc-300 font-mono-tabular placeholder:text-zinc-600 focus:border-sky-500/40 focus:outline-none lg:block"
                />
              )}
              {loading ? (
                <button
                  type="button"
                  onClick={handleCancel}
                  className="flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-rose-500/35 bg-rose-500/15 px-3 text-xs font-medium text-rose-200 transition-colors hover:bg-rose-500/25"
                >
                  <Square className="h-3 w-3 fill-current" strokeWidth={1.5} />
                  <span>Cancel</span>
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!query.trim()}
                  className="flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-sky-500/35 bg-sky-500/15 px-3 text-xs font-medium text-sky-200 transition-colors hover:bg-sky-500/25 disabled:opacity-40"
                >
                  <Send className="h-3.5 w-3.5" strokeWidth={1.5} />
                  <span>Ask</span>
                </button>
              )}
            </div>

            {errorMsg && (
              <div className="flex items-center justify-between gap-2 rounded border border-amber-500/25 bg-amber-500/[0.08] px-2.5 py-1.5 text-[11px] text-amber-200">
                <span>{errorMsg}</span>
                {verifierMode !== 'fts_fallback' && (
                  <button
                    type="button"
                    onClick={() => {
                      setVerifierMode('fts_fallback');
                      if (query.trim()) void runAsk(query, 'fts_fallback');
                    }}
                    className="shrink-0 rounded border border-amber-500/35 bg-amber-500/15 px-2 py-0.5 text-[10px] font-mono-tabular text-amber-200 hover:bg-amber-500/25"
                  >
                    Switch to Tier 0 FTS
                  </button>
                )}
              </div>
            )}
          </form>

          {/* Answer / Onboarding Body */}
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {loading && (
              <div className="mb-3 flex items-center justify-between rounded-md border border-sky-500/25 bg-sky-500/[0.07] px-3 py-2.5 text-xs text-sky-200">
                <span className="flex items-center gap-2 font-mono-tabular">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-400" strokeWidth={1.5} />
                  <span>Querying verified Cairnmind evidence &amp; checking file fingerprints…</span>
                </span>
                <button
                  type="button"
                  onClick={handleCancel}
                  className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-0.5 text-[11px] text-rose-300 hover:bg-rose-500/20"
                >
                  Cancel
                </button>
              </div>
            )}

            {/* Onboarding Quick-Prompts (always prominent before first question, compact bar afterwards) */}
            {!activeAnswer ? (
              <div className="flex flex-col gap-3">
                <div className="flex flex-col gap-1">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
                    Onboarding Quick-Prompts
                  </span>
                  <p className="text-xs text-zinc-400">
                    Zadaj pytanie z pierwszego dnia w repozytorium — odpowiedź opiera się wyłącznie o zweryfikowane cytaty z kodu lub twarde <code className="font-mono-tabular text-rose-300">UNKNOWN</code>.
                  </p>
                </div>

                <div className={cn('grid gap-2', compact ? 'grid-cols-1' : 'grid-cols-1 md:grid-cols-2')}>
                  {ONBOARDING_PROMPTS.map((item) => (
                    <button
                      key={item.query}
                      type="button"
                      disabled={loading}
                      onClick={() => void runAsk(item.query)}
                      className="group flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-white/[0.025] p-3 text-left transition-colors hover:border-sky-500/35 hover:bg-sky-500/[0.07] disabled:opacity-50"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-semibold text-zinc-100 group-hover:text-sky-200">
                          {item.title}
                        </span>
                        <GlassBadge tone={item.tag === 'Zero-Hallucination' ? 'rose' : 'sky'}>
                          {item.tag}
                        </GlassBadge>
                      </div>
                      <span className="text-xs text-zinc-400">„{item.query}”</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                {/* Compact quick-prompt chips for follow-up onboarding questions */}
                <div className="flex flex-wrap items-center gap-1.5">
                  {ONBOARDING_PROMPTS.map((item) => (
                    <button
                      key={item.query}
                      type="button"
                      disabled={loading}
                      onClick={() => void runAsk(item.query)}
                      className="rounded-full border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 text-[11px] text-zinc-400 transition-colors hover:border-sky-500/35 hover:bg-sky-500/10 hover:text-zinc-200"
                    >
                      {item.title}
                    </button>
                  ))}
                </div>

                {/* Active Answer Card */}
                <div className="flex flex-col gap-3 rounded-lg border border-white/[0.1] bg-zinc-950/70 p-3.5 shadow-[0_8px_24px_rgba(0,0,0,0.4)]">
                  <div className="flex flex-wrap items-start justify-between gap-2 border-b border-white/[0.06] pb-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <GlassBadge tone={statusTone(activeAnswer.status)}>
                          {activeAnswer.status === 'VERIFIED' ? (
                            <CheckCircle2 className="h-3 w-3" strokeWidth={1.5} />
                          ) : activeAnswer.status === 'PARTIAL' ? (
                            <AlertTriangle className="h-3 w-3" strokeWidth={1.5} />
                          ) : (
                            <HelpCircle className="h-3 w-3" strokeWidth={1.5} />
                          )}
                          <span>
                            {activeAnswer.status}
                            {activeAnswer.citations.length > 0
                              ? ` · ${activeAnswer.citations.length} ${activeAnswer.citations.length === 1 ? 'citation' : 'citations'}`
                              : ''}
                          </span>
                        </GlassBadge>
                        <GlassBadge tone="zinc">{activeAnswer.tier_used}</GlassBadge>
                        <GlassBadge tone="zinc">
                          <Clock className="h-3 w-3 text-zinc-500" strokeWidth={1.5} />
                          <span>{activeAnswer.latency_ms}ms</span>
                        </GlassBadge>
                        {activeAnswer.cached && <GlassBadge tone="sky">CACHED</GlassBadge>}
                      </div>
                      <h3 className="mt-1.5 text-sm font-semibold text-zinc-100">
                        {activeAnswer.query}
                      </h3>
                    </div>

                    {activeAnswer.status !== 'UNKNOWN' && (
                      <button
                        type="button"
                        disabled={!writable}
                        title={
                          writable
                            ? 'Przypnij tę zweryfikowaną odpowiedź jako fakt w pamięci projektu'
                            : readOnlyReason || 'Read-only mode: pinning facts is disabled'
                        }
                        onClick={() => onPinAsFact(activeAnswer)}
                        className="flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 px-2.5 text-[11px] font-medium text-sky-300 transition-colors hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <BookmarkPlus className="h-3.5 w-3.5" strokeWidth={1.5} />
                        <span>Pin as Fact</span>
                      </button>
                    )}
                  </div>

                  {activeAnswer.fallback_reason && (
                    <div className="flex items-center gap-2 rounded border border-amber-500/25 bg-amber-500/[0.07] px-2.5 py-1.5 text-[11px] text-amber-200">
                      <WifiOff className="h-3.5 w-3.5 shrink-0 text-amber-400" strokeWidth={1.5} />
                      <span>{activeAnswer.fallback_reason}</span>
                    </div>
                  )}

                  <p className="text-xs leading-relaxed text-zinc-200 whitespace-pre-wrap">
                    {activeAnswer.answer}
                  </p>

                  {activeAnswer.unknowns && activeAnswer.unknowns.length > 0 && (
                    <div className="rounded-md border border-rose-500/20 bg-rose-500/[0.05] p-2.5 text-[11px] text-rose-200">
                      <div className="font-semibold text-rose-300">Unknowns / Gaps</div>
                      <ul className="mt-1 list-disc pl-4 space-y-0.5">
                        {activeAnswer.unknowns.map((u) => (
                          <li key={u}>{u}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Expandable Verbatim Citations */}
                  <div className="flex flex-col gap-1.5 border-t border-white/[0.06] pt-2.5">
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedCitations((prev) => ({
                          ...prev,
                          [activeAnswer.id]: !(prev[activeAnswer.id] ?? true),
                        }))
                      }
                      className="flex items-center justify-between text-left text-[11px] font-medium uppercase tracking-wider text-zinc-400 hover:text-zinc-200"
                    >
                      <span className="flex items-center gap-1.5">
                        {(expandedCitations[activeAnswer.id] ?? true) ? (
                          <ChevronDown className="h-3.5 w-3.5" strokeWidth={1.5} />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5" strokeWidth={1.5} />
                        )}
                        <span>Verbatim Evidence Citations ({activeAnswer.citations.length})</span>
                      </span>
                      <span className="font-mono-tabular text-[10px] text-zinc-500">
                        Click citation to open Quote &amp; Fingerprint Inspector
                      </span>
                    </button>

                    {(expandedCitations[activeAnswer.id] ?? true) && (
                      <div className="flex flex-col gap-2 pt-1">
                        {activeAnswer.citations.length === 0 ? (
                          <div className="rounded border border-white/[0.06] bg-white/[0.02] p-2.5 text-xs text-zinc-500 font-mono-tabular">
                            0 verbatim citations — no fabricated references.
                          </div>
                        ) : (
                          activeAnswer.citations.map((cit) => {
                            const staleNow = isCitationStaleNow(cit);
                            return (
                              <button
                                key={cit.id}
                                type="button"
                                onClick={() => onSelectCitation(cit)}
                                className={cn(
                                  'group flex flex-col gap-1.5 rounded-md border p-2.5 text-left transition-colors',
                                  staleNow
                                    ? 'border-amber-500/45 bg-amber-500/[0.07] hover:bg-amber-500/[0.11]'
                                    : 'border-white/[0.08] bg-white/[0.02] hover:border-sky-500/35 hover:bg-sky-500/[0.06]',
                                )}
                              >
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <span className="flex min-w-0 items-center gap-1.5 font-mono-tabular text-[11px] text-sky-300">
                                    <FileCode2 className="h-3.5 w-3.5 shrink-0 text-sky-400" strokeWidth={1.5} />
                                    <span className="truncate">
                                      {cit.file_path}:{cit.line_start}-{cit.line_end}
                                    </span>
                                    <ExternalLink className="h-3 w-3 shrink-0 opacity-60 group-hover:opacity-100" strokeWidth={1.5} />
                                  </span>

                                  <div className="flex flex-wrap items-center gap-1.5">
                                    {staleNow && (
                                      <GlassBadge tone="amber" className="animate-pulse border-amber-400/50">
                                        <AlertTriangle className="h-3 w-3" strokeWidth={1.75} />
                                        <span>STALE NOW</span>
                                      </GlassBadge>
                                    )}
                                    <OriginBadge origin={cit.origin} compact />
                                    <span className="text-[10px] text-zinc-500 font-mono-tabular">
                                      fp:{shortHash(cit.fingerprint, 8)}
                                    </span>
                                  </div>
                                </div>

                                <pre className="overflow-x-auto rounded border border-white/[0.06] bg-zinc-950/80 px-2 py-1.5 text-[11px] leading-relaxed text-zinc-300 font-mono-tabular whitespace-pre-wrap">
                                  {cit.verbatim_quote}
                                </pre>
                              </button>
                            );
                          })
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Session History Rail (full view only) */}
        {!compact && (
          <div className="hidden w-64 shrink-0 flex-col overflow-hidden bg-zinc-950/35 lg:flex">
            <div className="flex h-9 shrink-0 items-center justify-between border-b border-white/[0.06] px-2.5">
              <span className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                <History className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
                <span>Session History ({history.length})</span>
              </span>
              {history.length > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    setHistory([]);
                    setActiveAnswerId(null);
                  }}
                  title="Clear session Ask history"
                  className="rounded p-1 text-zinc-500 hover:bg-white/[0.06] hover:text-zinc-300"
                >
                  <Trash2 className="h-3 w-3" strokeWidth={1.5} />
                </button>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {history.length === 0 ? (
                <div className="py-6 text-center text-[11px] text-zinc-600 font-mono-tabular">
                  No questions asked in this session yet.
                </div>
              ) : (
                <div className="flex flex-col gap-1">
                  {history.map((item) => {
                    const selected = activeAnswer?.id === item.id;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setActiveAnswerId(item.id)}
                        className={cn(
                          'flex flex-col gap-1 rounded-md border p-2 text-left transition-colors',
                          selected
                            ? 'border-sky-500/35 bg-sky-500/[0.09]'
                            : 'border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.05]',
                        )}
                      >
                        <div className="flex items-center justify-between gap-1.5">
                          <GlassBadge tone={statusTone(item.status)} className="text-[10px]">
                            {item.status}
                          </GlassBadge>
                          <span className="text-[10px] text-zinc-500 font-mono-tabular">
                            {formatShortTime(item.timestamp)}
                          </span>
                        </div>
                        <span className="line-clamp-2 text-[11px] text-zinc-200">{item.query}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
