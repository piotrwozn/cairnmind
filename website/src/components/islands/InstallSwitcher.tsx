import { useState } from 'react';
import { Check, Copy, Terminal } from 'lucide-react';
import installData from '@/data/install.json';

interface TabItem {
  id: string;
  label: string;
  badge: string;
  summary: string;
  blocks: { label: string; commands: string[] }[];
  note?: string;
}

const TABS: TabItem[] = [
  {
    id: installData.quickTry.id,
    label: installData.quickTry.label,
    badge: installData.quickTry.badge,
    summary: installData.quickTry.summary,
    blocks: [
      {
        label: '15-second sandbox + lightweight SQLite start',
        commands: installData.quickTry.commands,
      },
    ],
    note: installData.quickTry.note,
  },
  ...installData.platforms.map((p) => ({
    id: p.id,
    label: p.label,
    badge: p.badge,
    summary: p.summary,
    blocks: [
      { label: p.prereqLabel, commands: p.prereqCommands },
      { label: p.installLabel, commands: p.installCommands },
    ],
  })),
];

function CopyCodeBlock({ label, commands }: { label: string; commands: string[] }) {
  const [copied, setCopied] = useState(false);
  const text = commands.join('\n');

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {}
  };

  return (
    <div className="rounded-xl border border-[var(--glass-border)] bg-[var(--bg-code)] overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-3.5 py-2 border-b border-[var(--glass-border)] bg-[var(--glass-bg-subtle)]">
        <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-[var(--text-muted)]">
          <Terminal size={12} aria-hidden="true" />
          {label}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          aria-label={`Copy commands for ${label}`}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-mono border border-[var(--glass-border)] bg-[var(--glass-bg)] text-[var(--text-primary)] hover:border-[var(--accent-border)] cursor-pointer"
        >
          {copied ? <Check size={12} className="text-[var(--ok)]" aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="m-0 p-3.5 text-xs sm:text-[0.83rem] leading-relaxed overflow-x-auto text-[var(--text-strong)]">
        <code>{text}</code>
      </pre>
    </div>
  );
}

export default function InstallSwitcher() {
  const [activeId, setActiveId] = useState<string>(TABS[0].id);
  const active = TABS.find((t) => t.id === activeId) ?? TABS[0];

  return (
    <div className="glass-elevated p-5 sm:p-7">
      <div
        role="tablist"
        aria-label="Installation platforms"
        className="flex flex-wrap gap-2 pb-4 border-b border-[var(--glass-border)]"
      >
        {TABS.map((tab) => {
          const selected = tab.id === active.id;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={`install-tab-${tab.id}`}
              aria-selected={selected}
              aria-controls={`install-panel-${tab.id}`}
              onClick={() => setActiveId(tab.id)}
              className={`min-h-[38px] px-3.5 py-1.5 rounded-lg text-xs sm:text-sm font-medium cursor-pointer transition-colors border ${
                selected
                  ? 'bg-[var(--accent-surface)] text-[var(--text-strong)] border-[var(--accent-border)] font-semibold'
                  : 'bg-[var(--glass-bg-subtle)] text-[var(--text-muted)] border-[var(--glass-border)] hover:text-[var(--text-strong)]'
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      <div
        role="tabpanel"
        id={`install-panel-${active.id}`}
        aria-labelledby={`install-tab-${active.id}`}
        className="mt-5 space-y-4"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-[var(--text-primary)] max-w-2xl leading-relaxed">{active.summary}</p>
          <span className="glass-pill">{active.badge}</span>
        </div>

        <div className="grid grid-cols-1 gap-3.5">
          {active.blocks.map((block) => (
            <CopyCodeBlock key={block.label} label={block.label} commands={block.commands} />
          ))}
        </div>

        {active.note && (
          <p className="text-xs font-mono text-[var(--text-muted)]">{active.note}</p>
        )}
      </div>

      <noscript>
        <div className="mt-6 pt-5 border-t border-[var(--glass-border)] space-y-4">
          <p className="font-mono text-xs text-[var(--text-muted)]">
            JavaScript is off, so all platform install commands are shown below:
          </p>
          {TABS.slice(1).map((tab) => (
            <div key={tab.id} className="space-y-2">
              <p className="text-sm font-semibold text-[var(--text-strong)]">
                {tab.label} ({tab.badge})
              </p>
              {tab.blocks.map((b) => (
                <pre key={b.label} className="code-block">
                  <code>{b.commands.join('\n')}</code>
                </pre>
              ))}
            </div>
          ))}
        </div>
      </noscript>
    </div>
  );
}
