import { useState } from 'react';
import { Check, Copy, FileCode2, ShieldCheck } from 'lucide-react';
import type { ContractSample } from '@/lib/contracts';

interface Props {
  samples: ContractSample[];
  repoUrl: string;
}

export default function ProductInspector({ samples, repoUrl }: Props) {
  const [selectedId, setSelectedId] = useState<string>(samples[0]?.id ?? '');
  const [copied, setCopied] = useState(false);

  const active = samples.find((s) => s.id === selectedId) ?? samples[0];
  if (!active) return null;

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(active.command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {}
  };

  return (
    <div className="glass-elevated p-5 sm:p-7">
      <div className="flex flex-wrap items-center justify-between gap-3 pb-4 border-b border-[var(--glass-border)]">
        <div>
          <p className="eyebrow">Live Contract Explorer</p>
          <h3 className="mt-1 text-lg sm:text-xl font-semibold">
            Inspect real CI-frozen JSON &amp; MCP outputs
          </h3>
        </div>
        <span className="glass-pill glass-pill-ok">
          <ShieldCheck size={13} aria-hidden="true" />
          Loaded directly from tests/fixtures/contracts/
        </span>
      </div>

      {/* Selector buttons */}
      <div
        role="tablist"
        aria-label="Frozen Cairnmind contract samples"
        className="mt-4 flex flex-wrap gap-2"
      >
        {samples.map((sample) => {
          const isSelected = sample.id === active.id;
          return (
            <button
              key={sample.id}
              type="button"
              role="tab"
              id={`contract-tab-${sample.id}`}
              aria-selected={isSelected}
              aria-controls={`contract-panel-${sample.id}`}
              onClick={() => setSelectedId(sample.id)}
              className={`min-h-[38px] px-3 py-1.5 rounded-lg text-xs font-mono cursor-pointer transition-colors border ${
                isSelected
                  ? 'bg-[var(--accent-surface)] text-[var(--text-strong)] border-[var(--accent-border)] font-semibold'
                  : 'bg-[var(--glass-bg-subtle)] text-[var(--text-muted)] border-[var(--glass-border)] hover:text-[var(--text-strong)]'
              }`}
            >
              {sample.label}
            </button>
          );
        })}
      </div>

      {/* Active payload view */}
      <div
        role="tabpanel"
        id={`contract-panel-${active.id}`}
        aria-labelledby={`contract-tab-${active.id}`}
        className="mt-5 grid grid-cols-1 lg:grid-cols-12 gap-5"
      >
        <div className="lg:col-span-5 flex flex-col justify-between gap-4">
          <div className="space-y-3">
            <span className="glass-pill">{active.category}</span>
            <p className="text-sm text-[var(--text-primary)] leading-relaxed">{active.summary}</p>

            <div className="space-y-2.5 pt-1">
              {active.highlights.map((h) => (
                <div
                  key={h.field}
                  className="rounded-lg border border-[var(--glass-border)] bg-[var(--glass-bg-subtle)] p-3"
                >
                  <p
                    className={`font-mono text-xs font-semibold ${
                      h.tone === 'ok'
                        ? 'text-[var(--ok)]'
                        : h.tone === 'warn'
                          ? 'text-[var(--warn)]'
                          : 'text-[var(--accent)]'
                    }`}
                  >
                    {h.field}
                  </p>
                  <p className="mt-1 text-xs text-[var(--text-muted)] leading-snug">{h.meaning}</p>
                </div>
              ))}
            </div>
          </div>

          <div className="pt-3 border-t border-[var(--glass-border)] flex flex-wrap items-center justify-between gap-2 text-xs font-mono">
            <a
              href={`${repoUrl}/blob/main/${active.fixtureRelPath}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5"
            >
              <FileCode2 size={13} aria-hidden="true" />
              View fixture on GitHub ↗
            </a>
            <a href="/docs/stable-contract/">1.0 Contract →</a>
          </div>
        </div>

        <div className="lg:col-span-7 rounded-xl border border-[var(--glass-border)] bg-[var(--bg-code)] overflow-hidden flex flex-col">
          <div className="flex items-center justify-between gap-2 px-3.5 py-2.5 border-b border-[var(--glass-border)] bg-[var(--glass-bg-subtle)]">
            <code className="text-xs text-[var(--accent)] truncate" title={active.command}>
              $ {active.command}
            </code>
            <button
              type="button"
              onClick={copyCommand}
              aria-label={`Copy command ${active.command}`}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-mono border border-[var(--glass-border)] bg-[var(--glass-bg)] text-[var(--text-primary)] hover:border-[var(--accent-border)] cursor-pointer shrink-0"
            >
              {copied ? <Check size={12} className="text-[var(--ok)]" aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="m-0 p-4 text-[11.5px] leading-relaxed overflow-auto max-h-[380px] text-[var(--text-primary)]">
            <code>{active.payloadPretty}</code>
          </pre>
        </div>
      </div>

      {/* Zero-JS static summary of all 6 real contract samples */}
      <noscript>
        <div className="mt-6 pt-5 border-t border-[var(--glass-border)] grid grid-cols-1 md:grid-cols-2 gap-3">
          {samples.slice(1).map((s) => (
            <div key={s.id} className="p-3.5 rounded-lg border border-[var(--glass-border)] bg-[var(--glass-bg-subtle)]">
              <p className="font-mono text-xs text-[var(--accent)]">{s.command}</p>
              <p className="mt-1 text-xs text-[var(--text-muted)]">{s.summary}</p>
            </div>
          ))}
        </div>
      </noscript>
    </div>
  );
}
