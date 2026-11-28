#!/usr/bin/env python3
"""Generate the 60-second split-screen before/after SVG demo for README.md.

The scenario mirrors ``examples/sample-project`` (Trail Notes):
  0:00-0:20  Hallucination vs. ``unknown`` on a nonexistent ``--sync-interval`` flag.
  0:20-0:45  False ``done`` vs. blocked ``done`` after editing code past the last test run.
  0:45-1:00  Re-running the check profile -> verified ``done`` with a fresh code fingerprint.

Run:
    uv run python scripts/build_demo_svg.py
    uv run python scripts/build_demo_svg.py --check
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "assets" / "demo-60s.svg"


def build_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 980 460" width="980" height="460" role="img" aria-labelledby="title desc">
  <title id="title">Cairnmind 60-second demo: without Cairnmind vs. with Cairnmind</title>
  <desc id="desc">Split-screen comparison over 60 seconds: 0:00-0:20 hallucinated flag vs unknown; 0:20-0:45 false done vs blocked stale tests; 0:45-1:00 fresh check run and verified done.</desc>
  <defs>
    <style>
      .bg { fill: #0d1117; }
      .panel { fill: #161b22; stroke: #30363d; stroke-width: 1; }
      .header-bad { fill: #261419; }
      .header-good { fill: #12231b; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; font-size: 12.5px; }
      .sans { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
      .title { font-size: 14px; font-weight: 700; fill: #e6edf3; }
      .badge-bad { fill: #f85149; font-weight: 700; font-size: 12px; }
      .badge-good { fill: #3fb950; font-weight: 700; font-size: 12px; }
      .muted { fill: #8b949e; }
      .text { fill: #e6edf3; }
      .cmd { fill: #79c0ff; }
      .bad { fill: #ff7b72; }
      .good { fill: #56d364; }
      .warn { fill: #d29922; }
      .act-label { font-size: 11.5px; font-weight: 600; fill: #8b949e; }
      .bar-track { fill: #21262d; }
      .bar-fill { fill: #58a6ff; }

      /* 60-second loop: Act 1 (0-33%), Act 2 (33-75%), Act 3 (75-100%) */
      @keyframes progress {
        0% { width: 0px; }
        100% { width: 920px; }
      }
      @keyframes act1 {
        0%, 31% { opacity: 1; }
        33%, 98% { opacity: 0.28; }
        100% { opacity: 1; }
      }
      @keyframes act2 {
        0%, 31% { opacity: 0.22; }
        34%, 73% { opacity: 1; }
        76%, 100% { opacity: 0.28; }
      }
      @keyframes act3 {
        0%, 73% { opacity: 0.22; }
        76%, 98% { opacity: 1; }
        100% { opacity: 0.22; }
      }
      .progress-bar { animation: progress 18s linear infinite; }
      .scene-1 { animation: act1 18s ease-in-out infinite; }
      .scene-2 { animation: act2 18s ease-in-out infinite; }
      .scene-3 { animation: act3 18s ease-in-out infinite; }

      @media (prefers-reduced-motion: reduce) {
        .progress-bar { animation: none; width: 920px; }
        .scene-1, .scene-2, .scene-3 { animation: none; opacity: 1; }
      }
    </style>
  </defs>

  <!-- Outer frame -->
  <rect class="bg" x="0" y="0" width="980" height="460" rx="12" />

  <!-- Top timeline bar -->
  <text class="sans title" x="30" y="30">Trail Notes (examples/sample-project) — 60-second before &amp; after</text>
  <text class="mono muted" x="742" y="30">0:00–0:20 · 0:20–0:45 · 0:45–1:00</text>
  <rect class="bar-track" x="30" y="42" width="920" height="5" rx="2.5" />
  <rect class="bar-fill progress-bar" x="30" y="42" width="0" height="5" rx="2.5" />

  <!-- Left pane: Without Cairnmind -->
  <rect class="panel" x="30" y="62" width="448" height="374" rx="8" />
  <path class="header-bad" d="M38 62 H470 A8 8 0 0 1 478 70 V94 H30 V70 A8 8 0 0 1 38 62 Z" />
  <circle cx="48" cy="78" r="5" fill="#f85149" />
  <text class="sans badge-bad" x="62" y="82">WITHOUT CAIRNMIND (notes / plain agent)</text>

  <!-- Right pane: With Cairnmind -->
  <rect class="panel" x="502" y="62" width="448" height="374" rx="8" />
  <path class="header-good" d="M510 62 H942 A8 8 0 0 1 950 70 V94 H502 V70 A8 8 0 0 1 510 62 Z" />
  <circle cx="520" cy="78" r="5" fill="#3fb950" />
  <text class="sans badge-good" x="534" y="82">WITH CAIRNMIND (memory as evidence)</text>

  <!-- ACT 1: 0:00-0:20 Hallucination vs unknown -->
  <g class="scene-1">
    <!-- Left -->
    <text class="sans act-label" x="46" y="116">0:00–0:20 · Question about nonexistent flag</text>
    <text class="mono cmd" x="46" y="136">&gt; How does --sync-interval control cloud sync?</text>
    <text class="mono bad" x="46" y="156">Agent: Pass --sync-interval 300 to write_note()</text>
    <text class="mono bad" x="46" y="173">       to sync Trail Notes every 5 minutes.</text>
    <text class="mono muted" x="46" y="191">✗ Confident hallucination (flag does not exist)</text>

    <!-- Right -->
    <text class="sans act-label" x="518" y="116">0:00–0:20 · Verbatim evidence gate</text>
    <text class="mono cmd" x="518" y="136">$ cairnmind ask trail-notes "sync-interval flag?"</text>
    <text class="mono good" x="518" y="156">unknown — no verbatim confirmation in the</text>
    <text class="mono good" x="518" y="173">current project files.</text>
    <text class="mono muted" x="518" y="191">✓ No quote in trail_notes/store.py → refuses to guess</text>
  </g>

  <!-- Divider 1 -->
  <line x1="46" y1="206" x2="462" y2="206" stroke="#21262d" stroke-width="1" />
  <line x1="518" y1="206" x2="934" y2="206" stroke="#21262d" stroke-width="1" />

  <!-- ACT 2: 0:20-0:45 False done vs blocked -->
  <g class="scene-2">
    <!-- Left -->
    <text class="sans act-label" x="46" y="226">0:20–0:45 · Code edited after earlier tests</text>
    <text class="mono muted" x="46" y="246"># edited trail_notes/store.py (bug introduced)</text>
    <text class="mono cmd" x="46" y="264">&gt; Mark step "store-tags" as done.</text>
    <text class="mono bad" x="46" y="284">Agent: Unit tests passed earlier — status: done.</text>
    <text class="mono muted" x="46" y="302">✗ Closes task on stale test output</text>

    <!-- Right -->
    <text class="sans act-label" x="518" y="226">0:20–0:45 · Code fingerprint check on done</text>
    <text class="mono muted" x="518" y="246"># edited trail_notes/store.py (fingerprint changed)</text>
    <text class="mono cmd" x="518" y="264">$ cairnmind task update trail-notes t1 done</text>
    <text class="mono warn" x="518" y="284">Blocked: code changed after the last check run.</text>
    <text class="mono warn" x="518" y="302">Run the checks (cairnmind check run) and try again.</text>
  </g>

  <!-- Divider 2 -->
  <line x1="46" y1="318" x2="462" y2="318" stroke="#21262d" stroke-width="1" />
  <line x1="518" y1="318" x2="934" y2="318" stroke="#21262d" stroke-width="1" />

  <!-- ACT 3: 0:45-1:00 Fresh check -> verified done -->
  <g class="scene-3">
    <!-- Left -->
    <text class="sans act-label" x="46" y="338">0:45–1:00 · Next session inherits broken state</text>
    <text class="mono bad" x="46" y="360">Next agent trusts "done" from notes.</text>
    <text class="mono bad" x="46" y="378">Regression surfaces only later in CI or review.</text>
    <text class="mono muted" x="46" y="408">No quote check · No fingerprint · No proof</text>

    <!-- Right -->
    <text class="sans act-label" x="518" y="338">0:45–1:00 · Fresh check pins code fingerprint</text>
    <text class="mono cmd" x="518" y="358">$ cairnmind check run trail-notes unit --task t1</text>
    <text class="mono good" x="518" y="376">✓ unit: passed  (fingerprint: sha256:7c9e…f41a)</text>
    <text class="mono cmd" x="518" y="394">$ cairnmind task update trail-notes t1 done</text>
    <text class="mono good" x="518" y="412">✓ state: done  (fresh machine proof attached)</text>
  </g>
</svg>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or verify docs/assets/demo-60s.svg")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the committed SVG differs.")
    args = parser.parse_args(argv)
    rendered = build_svg()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"{OUTPUT} is out of date; run `uv run python scripts/build_demo_svg.py`.", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT} ({len(rendered.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
