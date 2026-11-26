import { Command } from 'cmdk';
import { AnimatePresence, motion } from 'framer-motion';
import {
  FileCode2,
  FileSearch,
  GitFork,
  Layers,
  RefreshCw,
  Search,
  ShieldAlert,
} from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { shortHash } from '@/lib/utils';
import type { ActiveView, InspectorSelection, UiSnapshot } from '@/types/contracts';

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  snapshot: UiSnapshot | undefined;
  onSelectView: (view: ActiveView) => void;
  onSelectEntity: (view: ActiveView, selection: InspectorSelection) => void;
  onReverify: () => void;
}

export function CommandPalette({
  open,
  onClose,
  snapshot,
  onSelectView,
  onSelectEntity,
  onReverify,
}: CommandPaletteProps) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-[12vh] backdrop-blur-sm"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: -8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -8 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            className="w-full max-w-[640px] overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/80 shadow-[0_16px_48px_rgba(0,0,0,0.65),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            <Command label="Cairnmind Command Palette" className="flex flex-col">
              <div className="flex items-center gap-2 border-b border-white/[0.08] px-3 py-2.5">
                <Search className="h-4 w-4 text-zinc-500" strokeWidth={1.5} />
                <Command.Input
                  autoFocus
                  placeholder="Search verbatim quotes, SHA-256 hashes, tasks, files, audit rules..."
                  className="w-full bg-transparent text-sm text-zinc-100 placeholder:text-zinc-500 focus:outline-none"
                />
                <kbd className="rounded border border-white/[0.1] bg-zinc-950/70 px-1.5 py-0.5 text-[10px] text-zinc-500 font-mono-tabular">
                  Esc
                </kbd>
              </div>

              <Command.List className="max-h-[380px] overflow-y-auto p-1.5">
                <Command.Empty className="py-8 text-center text-xs text-zinc-500 font-mono-tabular">
                  No matching tasks, quotes, SHA-256 hashes, or audit events.
                </Command.Empty>

                <Command.Group
                  heading="Quick Actions & Views"
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-zinc-500"
                >
                  <Command.Item
                    value="action reverify fingerprints scan hashes"
                    onSelect={() => {
                      onReverify();
                      onClose();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <RefreshCw className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                      <span>Re-verify Fingerprints</span>
                    </span>
                    <span className="text-[10px] text-zinc-500 font-mono-tabular">POST /actions/reverify</span>
                  </Command.Item>

                  <Command.Item
                    value="view plan concurrency matrix tasks sessions"
                    onSelect={() => {
                      onSelectView('matrix');
                      onClose();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <Layers className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                      <span>Switch to Plan &amp; Concurrency Matrix</span>
                    </span>
                    <kbd className="text-[10px] text-zinc-500 font-mono-tabular">1</kbd>
                  </Command.Item>

                  <Command.Item
                    value="view provenance staleness quotes inspector"
                    onSelect={() => {
                      onSelectView('provenance');
                      onClose();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <FileSearch className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                      <span>Switch to Provenance &amp; Staleness Inspector</span>
                    </span>
                    <kbd className="text-[10px] text-zinc-500 font-mono-tabular">2</kbd>
                  </Command.Item>

                  <Command.Item
                    value="view dependency blast radius graph"
                    onSelect={() => {
                      onSelectView('graph');
                      onClose();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <GitFork className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                      <span>Switch to Dependency &amp; Blast-Radius Graph</span>
                    </span>
                    <kbd className="text-[10px] text-zinc-500 font-mono-tabular">3</kbd>
                  </Command.Item>

                  <Command.Item
                    value="view guardrail audit log mcp rejections firewall"
                    onSelect={() => {
                      onSelectView('audit');
                      onClose();
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                  >
                    <span className="flex items-center gap-2">
                      <ShieldAlert className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
                      <span>Switch to Guardrail &amp; Rejection Audit Log</span>
                    </span>
                    <kbd className="text-[10px] text-zinc-500 font-mono-tabular">4</kbd>
                  </Command.Item>
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
                          onClose();
                        }}
                        className="flex cursor-pointer items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-xs text-zinc-200 aria-selected:bg-white/[0.08]"
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="shrink-0 text-sky-400 font-mono-tabular">{task.id}</span>
                          <span className="truncate">{task.title}</span>
                        </span>
                        <GlassBadge tone={statusToTone(task.test_gate.status)}>
                          {task.test_gate.status}
                        </GlassBadge>
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
                          onClose();
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
                        <GlassBadge tone={statusToTone(claim.provenance_status)}>
                          {claim.provenance_status}
                        </GlassBadge>
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
                          onClose();
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
