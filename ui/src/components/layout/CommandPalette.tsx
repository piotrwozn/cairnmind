import { useState } from 'react';
import { Command } from 'cmdk';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Activity,
  BookmarkPlus,
  Compass,
  Download,
  FileCode2,
  FilePlus2,
  FileSearch,
  GitFork,
  Layers,
  ListPlus,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
} from 'lucide-react';
import type { DraftModalState } from '@/components/drafts/DraftModals';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { OriginBadge } from '@/components/glass/OriginBadge';
import { shortHash } from '@/lib/utils';
import type { ActiveView, InspectorSelection, UiSnapshot } from '@/types/contracts';

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  snapshot: UiSnapshot | undefined;
  onSelectView: (view: ActiveView) => void;
  onSelectEntity: (view: ActiveView, selection: InspectorSelection) => void;
  onReverify: () => void;
  onReindex: () => void;
  onOpenDoctor: () => void;
  onOpenExport: () => void;
  onAskQuestion: (query: string) => void;
  onOpenAskDrawer: () => void;
  onOpenDraftModal: (modal: DraftModalState) => void;
}

export function CommandPalette({
  open,
  onClose,
  snapshot,
  onSelectView,
  onSelectEntity,
  onReverify,
  onReindex,
  onOpenDoctor,
  onOpenExport,
  onAskQuestion,
  onOpenAskDrawer,
  onOpenDraftModal,
}: CommandPaletteProps) {
  const [searchText, setSearchText] = useState('');
  const writable = snapshot?.capabilities?.writable ?? true;

  const closeAndReset = () => {
    setSearchText('');
    onClose();
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-[10vh] backdrop-blur-sm"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closeAndReset();
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: -8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -8 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            className="w-full max-w-[660px] overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/85 shadow-[0_16px_48px_rgba(0,0,0,0.65),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            <Command label="Cairnmind Command Palette" className="flex flex-col">
              <div className="flex items-center gap-2 border-b border-white/[0.08] px-3 py-2.5">
                <Search className="h-4 w-4 text-zinc-500" strokeWidth={1.5} />
                <Command.Input
                  autoFocus
                  value={searchText}
                  onValueChange={setSearchText}
                  placeholder="Ask Cairnmind, create draft/task/fact, or search quotes, hashes, tasks..."
                  className="w-full bg-transparent text-sm text-zinc-100 placeholder:text-zinc-500 focus:outline-none"
                />
                <kbd className="rounded border border-white/[0.1] bg-zinc-950/70 px-1.5 py-0.5 text-[10px] text-zinc-500 font-mono-tabular">
                  Esc
                </kbd>
              </div>

              <Command.List className="max-h-[420px] overflow-y-auto p-1.5">
                <Command.Empty className="py-6 text-center text-xs text-zinc-500 font-mono-tabular">
                  {searchText.trim() ? (
                    <button
                      type="button"
                      onClick={() => {
                        const q = searchText.trim();
                        closeAndReset();
                        onAskQuestion(q);
                      }}
                      className="inline-flex items-center gap-2 rounded-md border border-sky-500/35 bg-sky-500/15 px-3 py-1.5 text-xs text-sky-200 hover:bg-sky-500/25"
                    >
                      <Sparkles className="h-3.5 w-3.5" strokeWidth={1.5} />
                      <span>Ask Cairnmind: &ldquo;{searchText.trim()}&rdquo;</span>
                    </button>
                  ) : (
                    'No matching tasks, quotes, SHA-256 hashes, or audit events.'
                  )}
                </Command.Empty>

                {/* Interactive Ask & Draft Creation */}
                <Command.Group
                  heading="Interactive Ask & Draft Cockpit Actions"
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-zinc-500"
                >
                  {searchText.trim().length > 2 && (
                    <Command.Item
                      value={`ask question query ${searchText}`}
                      onSelect={() => {
                        const q = searchText.trim();
                        closeAndReset();
                        onAskQuestion(q);
                      }}
                      className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-sky-200 aria-selected:bg-sky-500/15"
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <Sparkles className="h-3.5 w-3.5 shrink-0 text-sky-400" strokeWidth={1.5} />
                        <span className="truncate">Ask Cairnmind: &ldquo;{searchText.trim()}&rdquo;</span>
                      </span>
                      <span className="shrink-0 text-[10px] text-sky-400 font-mono-tabular">POST /api/ask</span>
                    </Command.Item>
                  )}

                  <Command.Item
                    value="open ask drawer onboarding question"
                    onSelect={() => {
                      closeAndReset();
                      onOpenAskDrawer();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <Sparkles className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                      <span>Open Quick Ask Drawer</span>
                    </span>
                    <span className="text-[10px] text-zinc-500 font-mono-tabular">Ask</span>
                  </Command.Item>

                  {writable && (
                    <>
                      <Command.Item
                        value="new draft plan jira stage"
                        onSelect={() => {
                          closeAndReset();
                          onOpenDraftModal({ kind: 'plan', editingPlan: null });
                        }}
                        className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex items-center gap-2">
                          <FilePlus2 className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                          <span>New Draft Plan (Jira / Spec)</span>
                        </span>
                        <span className="text-[10px] text-zinc-500 font-mono-tabular">POST /api/plans</span>
                      </Command.Item>

                      <Command.Item
                        value="quick task single standalone draft"
                        onSelect={() => {
                          closeAndReset();
                          onOpenDraftModal({ kind: 'task' });
                        }}
                        className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex items-center gap-2">
                          <ListPlus className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                          <span>Quick Single Task</span>
                        </span>
                        <span className="text-[10px] text-zinc-500 font-mono-tabular">POST /api/tasks</span>
                      </Command.Item>

                      <Command.Item
                        value="add fact decision constraint memory"
                        onSelect={() => {
                          closeAndReset();
                          onOpenDraftModal({ kind: 'fact' });
                        }}
                        className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex items-center gap-2">
                          <BookmarkPlus className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
                          <span>Add Decision / Fact</span>
                        </span>
                        <span className="text-[10px] text-zinc-500 font-mono-tabular">POST /api/facts</span>
                      </Command.Item>
                    </>
                  )}
                </Command.Group>

                {/* Operational Quick Actions */}
                <Command.Group
                  heading="Operations & Views"
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-zinc-500"
                >
                  {writable && (
                    <>
                      <Command.Item
                        value="action reindex repository scan sse progress"
                        onSelect={() => {
                          closeAndReset();
                          onReindex();
                        }}
                        className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex items-center gap-2">
                          <RefreshCw className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                          <span>Re-index Repository (SSE progress)</span>
                        </span>
                        <span className="text-[10px] text-zinc-500 font-mono-tabular">POST /api/actions/reindex</span>
                      </Command.Item>

                      <Command.Item
                        value="action reverify fingerprints scan hashes"
                        onSelect={() => {
                          closeAndReset();
                          onReverify();
                        }}
                        className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex items-center gap-2">
                          <RefreshCw className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                          <span>Re-verify Fingerprints</span>
                        </span>
                        <span className="text-[10px] text-zinc-500 font-mono-tabular">POST /actions/reverify</span>
                      </Command.Item>
                    </>
                  )}

                  <Command.Item
                    value="action doctor healthcheck storage thresholds"
                    onSelect={() => {
                      closeAndReset();
                      onOpenDoctor();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <Activity className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
                      <span>Run Doctor / Healthcheck</span>
                    </span>
                    <span className="text-[10px] text-zinc-500 font-mono-tabular">GET /api/doctor</span>
                  </Command.Item>

                  <Command.Item
                    value="action export safe snapshot team redaction gate"
                    onSelect={() => {
                      closeAndReset();
                      onOpenExport();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <Download className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                      <span>Export Safe Snapshot (team)</span>
                    </span>
                    <span className="text-[10px] text-zinc-500 font-mono-tabular">Redaction Gate</span>
                  </Command.Item>

                  {[
                    { id: 'matrix' as const, label: 'Plan & Concurrency Matrix', key: '1', icon: Layers },
                    { id: 'provenance' as const, label: 'Provenance & Staleness Inspector', key: '2', icon: FileSearch },
                    { id: 'graph' as const, label: 'Dependency & Blast-Radius Graph', key: '3', icon: GitFork },
                    { id: 'audit' as const, label: 'Guardrail & Rejection Audit Log', key: '4', icon: ShieldAlert },
                    { id: 'ask' as const, label: 'Interactive Ask & Onboarding', key: '5', icon: Sparkles },
                    { id: 'drift' as const, label: 'Plan & Drift Monitor', key: '6', icon: Compass },
                    { id: 'drafts' as const, label: 'Draft State Manager', key: '7', icon: FilePlus2 },
                  ].map((v) => {
                    const Icon = v.icon;
                    return (
                      <Command.Item
                        key={v.id}
                        value={`view switch ${v.label} ${v.id}`}
                        onSelect={() => {
                          onSelectView(v.id);
                          closeAndReset();
                        }}
                        className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex items-center gap-2">
                          <Icon className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                          <span>Switch to {v.label}</span>
                        </span>
                        <kbd className="text-[10px] text-zinc-500 font-mono-tabular">{v.key}</kbd>
                      </Command.Item>
                    );
                  })}
                </Command.Group>

                {snapshot && snapshot.tasks.length > 0 && (
                  <Command.Group
                    heading="Plan Tasks"
                    className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-zinc-500"
                  >
                    {snapshot.tasks.map((task) => (
                      <Command.Item
                        key={task.id}
                        value={`task ${task.id} ${task.title} ${task.test_gate.profile_name} ${task.test_gate.sha256_short ?? ''}`}
                        onSelect={() => {
                          onSelectEntity('matrix', { kind: 'task', id: task.id });
                          closeAndReset();
                        }}
                        className="flex cursor-pointer items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="shrink-0 text-sky-400 font-mono-tabular">{task.id}</span>
                          <span className="truncate">{task.title}</span>
                        </span>
                        <div className="flex shrink-0 items-center gap-1.5">
                          <OriginBadge origin={task.origin} compact />
                          <GlassBadge tone={statusToTone(task.test_gate.status)}>
                            {task.test_gate.status}
                          </GlassBadge>
                        </div>
                      </Command.Item>
                    ))}
                  </Command.Group>
                )}

                {snapshot && snapshot.claims.length > 0 && (
                  <Command.Group
                    heading="Claims, Verbatim Quotes & SHA-256 Hashes"
                    className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-zinc-500"
                  >
                    {snapshot.claims.map((claim) => (
                      <Command.Item
                        key={claim.id}
                        value={`claim ${claim.id} ${claim.key} ${claim.title} ${claim.source_path} ${claim.verbatim_quote} ${claim.recorded_sha256} ${claim.current_disk_sha256}`}
                        onSelect={() => {
                          onSelectEntity('provenance', { kind: 'claim', id: claim.id });
                          closeAndReset();
                        }}
                        className="flex cursor-pointer items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <FileCode2 className="h-3.5 w-3.5 shrink-0 text-zinc-400" strokeWidth={1.5} />
                          <span className="truncate font-mono-tabular text-zinc-200">{claim.key}</span>
                          <span className="truncate text-[11px] text-zinc-500 font-mono-tabular">
                            {shortHash(claim.current_disk_sha256, 8)}
                          </span>
                        </span>
                        <div className="flex shrink-0 items-center gap-1.5">
                          <OriginBadge origin={claim.origin} compact />
                          <GlassBadge tone={statusToTone(claim.provenance_status)}>
                            {claim.provenance_status}
                          </GlassBadge>
                        </div>
                      </Command.Item>
                    ))}
                  </Command.Group>
                )}

                {snapshot && snapshot.audit_log.length > 0 && (
                  <Command.Group
                    heading="Guardrail Audit Entries"
                    className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-zinc-500"
                  >
                    {snapshot.audit_log.map((entry) => (
                      <Command.Item
                        key={entry.id}
                        value={`audit ${entry.id} ${entry.session_id} ${entry.mcp_tool} ${entry.reason_code} ${entry.message}`}
                        onSelect={() => {
                          onSelectEntity('audit', { kind: 'audit', id: entry.id });
                          closeAndReset();
                        }}
                        className="flex cursor-pointer items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="shrink-0 font-mono-tabular text-zinc-400">{entry.mcp_tool}</span>
                          <span className="truncate text-zinc-300">{entry.reason_code}</span>
                        </span>
                        <GlassBadge tone={statusToTone(entry.verdict)}>{entry.verdict}</GlassBadge>
                      </Command.Item>
                    ))}
                  </Command.Group>
                )}
              </Command.List>
            </Command>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
