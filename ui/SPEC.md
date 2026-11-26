# Cairnmind UI — Architecture & Design Specification (`UI_SPEC.md`)

## 1. Overview & Philosophy
`cairnmind/ui` is a local, single-project **State, Provenance & Concurrency Inspector** served directly by the Cairnmind backend (e.g., `cairnmind ui --port 4310`).

- **Core Role:** 95% Read-Only Observability (inspecting OCC locks, verbatim quote provenance, file fingerprint staleness, ArcadeDB blast radius, and MCP guardrail rejections) + 5% Emergency Control (dropping dead session locks, triggering fingerprint re-verification).
- **Aesthetic Direction:** **Obsidian Precision Glass** — unified with the Cairnmind brand website. Deep dark translucent layers, crisp hairline specular borders, subtle ambient depth, and physics-based micro-interactions (`framer-motion`), combined with high-density developer ergonomics.

---

## 2. Tech Stack (Monorepo: `ui/`)
- **Framework:** React 19 + TypeScript (strict mode) + Vite
- **Styling:** Tailwind CSS + `shadcn/ui` (customized for translucent glass surfaces)
- **Animations:** `framer-motion` (layout transitions, spring physics, live event pulses)
- **Data Tables:** `@tanstack/react-table` (high-density, keyboard-navigable)
- **Graph Visualization:** `@xyflow/react` (React Flow) + `dagre` (deterministic left-to-right layered layout)
- **State & Live Sync:** `@tanstack/react-query` + native `EventSource` (SSE)
- **Icons & Command Palette:** `lucide-react` (`strokeWidth={1.5}`) + `cmdk` (`Cmd+K`)

---

## 3. Design System: "Obsidian Precision Glass" (Anti-Vibecode Rules)

### 3.1. Visual Guardrails (STRICTLY ENFORCED)
1. **NO "AI Slop" Gradients:** Never use saturated multi-color neon blobs (`from-purple-500 via-pink-500 to-blue-500`), glowing outer box-shadows on every card, or floating decorative 3D shapes.
2. **NO Marketing Padding:** Avoid giant hero headers, `p-8` card padding, or `rounded-3xl` pill containers inside the workspace. Maximum border radius is `rounded-lg` (`8px`) for outer floating glass shells and `rounded-md` (`6px`) for inner controls.
3. **NO Emojis in UI:** Use `lucide-react` icons (`14px`–`16px`) exclusively.
4. **High Information Density:** Base UI text is `text-xs` (`12px`) and `text-sm` (`13px`). All system identifiers, SHA-256 hashes, file paths, `state_rev` numbers, timestamps, and verbatim quotes MUST use a monospace font (`Geist Mono` or `JetBrains Mono`) with `tabular-nums`.

### 3.2. Glass Surface Tokens
- **Canvas Background (`AppBg`):** `#060709` (`bg-[#060709]`) with an ultra-subtle radial ambient vignette at the top center (`radial-gradient(ellipse 60% 35% at 50% 0%, rgba(56, 189, 248, 0.06), transparent 70%)`) and a faint `1px` dot/grid matrix (`opacity-[0.03]`).
- **Primary Glass Panel (`GlassSurface`):**
  `bg-zinc-950/65 backdrop-blur-xl border border-white/[0.08] shadow-[0_8px_32px_rgba(0,0,0,0.45),inset_0_1px_0_0_rgba(255,255,255,0.06)]`
- **Elevated Glass Overlay (`Drawer / Cmd+K / Tooltip`):**
  `bg-zinc-900/75 backdrop-blur-2xl border border-white/[0.12] shadow-[0_16px_48px_rgba(0,0,0,0.65),inset_0_1px_0_0_rgba(255,255,255,0.1)]`
- **Interactive Row / Node Hover:**
  `hover:bg-white/[0.04] active:bg-white/[0.06] transition-colors duration-150`

### 3.3. Semantic Status Palette (Color = Signal Only)
Colors outside neutral zinc/white are reserved strictly for system truth states:
- **Verified / Valid (`emerald`):** `text-emerald-400 bg-emerald-500/10 border-emerald-500/25` (Quote verified, hash matches, test gate passed).
- **Stale / Outdated (`amber`):** `text-amber-400 bg-amber-500/10 border-amber-500/25` (File modified after quote/test capture, or session holding outdated `ack_rev`).
- **Rejected / Conflict (`rose`):** `text-rose-400 bg-rose-500/10 border-rose-500/25` (OCC write rejected, quote mismatch, missing test proof).
- **Active Agent / Focus (`sky`):** `text-sky-400 bg-sky-500/10 border-sky-500/25` (Active MCP session, selected graph node).

### 3.4. Motion & Animation Spec (`framer-motion`)
Animations must feel crisp, deterministic, and fast (never sluggish or bouncy):
- **Spring Physics Default:** `{ type: "spring", stiffness: 420, damping: 34, mass: 0.8 }`
- **Shared Layout Indicators:** Use `layoutId="active-nav-pill"` and `layoutId="active-tab-indicator"` for smooth sliding glass highlights across navigation and filter tabs.
- **Inspector & Console Transitions:** Slide and fade Right Inspector Drawer and Bottom Console with `AnimatePresence` (`opacity: 0 -> 1`, `x: 16 -> 0` in `<180ms`).
- **Real-Time SSE State Flashes:**
  - When a file edit flips an evidence item to `STALE`, trigger a subtle 600ms amber border pulse (`boxShadow` keyframe) on the affected row/node.
  - When an MCP write is `REJECTED`, animate the new audit row sliding into the top of the stream with a brief rose glass highlight.
- **Reduced Motion:** Respect `prefers-reduced-motion` automatically via `<MotionConfig reducedMotion="user">`.

---

## 4. Application Layout (Viewport-Locked IDE Shell)
Full-screen locked viewport (`h-screen w-screen overflow-hidden p-2 gap-2 flex flex-col`) where floating glass panels sit over the dark ambient canvas:

1. **Top Glass Status Bar (`h-10`):**
   - Project name & repository root path (monospace).
   - Live SSE connection pill (`LIVE` pulsing emerald dot vs `RECONNECTING` amber).
   - Global state revision counter (`state_rev: #42`).
   - Active MCP sessions badge + `Cmd+K` trigger button (`Search quotes, hashes, tasks...`).
2. **Middle Resizable Workspace (`flex-1`, `ResizablePanelGroup`):**
   - **Left Navigation Rail (`w-14` collapsed / `w-52` expanded):** Switches between the 4 core views + global quick-toggle `[ ! Only Stale & Conflicts ]`.
   - **Center Main Stage (`flex-1`):** Renders the active view inside a `GlassSurface` container.
   - **Right Inspector Drawer (`w-[440px]`, collapsible):** Context-aware detail view for any selected Task, Evidence Claim, Graph Node, or Audit Log entry.
3. **Bottom Live Event Stream (`h-40`, collapsible glass dock):**
   - Real-time tail of MCP tool calls, `resume_packet` acknowledgements, file watcher hash updates, and guardrail blocks.

---

## 5. Core Views & Functional Requirements

### View 1: Plan & Concurrency Matrix (Default)
- **Active Agent Sessions Strip (Top):** Horizontal glass cards for each connected MCP session (e.g., `claude-code`, `codex-cli`). Shows `session_id`, `acknowledged_rev`, and live OCC alignment:
  - If `acknowledged_rev === current_state_rev`: `SYNCED` (emerald).
  - If `acknowledged_rev < current_state_rev`: `OUTDATED_ACK — Next write will reject` (amber warning badge).
  - Action: `Drop Session` button for orphaned/stuck sessions.
- **Task & Gate Tree Table (Main):**
  - Columns: `Task ID`, `Title`, `Status`, `Evidence Health` (`X/Y Verified` progress pill), `Test Gate` (`Profile Name` + `PASS @ sha256_short` or `STALE_CODE`), `Updated At`.
  - Header Action: `Re-verify Fingerprints` button (triggers server-side file hash scan).

### View 2: Provenance & Staleness Inspector
- **Split Master-Detail List:** Filterable list of all stored claims, architectural decisions, and extracted memories (`ALL` | `VERIFIED` | `STALE`).
- **Verbatim Quote Code Viewer (in Right Inspector):**
  - Displays source file path, line range, `recorded_sha256` vs `current_disk_sha256`.
  - Renders the actual code snippet with the **exact `verbatim_quote` highlighted** inside a frosted glass code box.
  - If `STALE`, clearly indicates whether the file hash drifted while the quote still exists, or if the quote itself was mutated/deleted.

### View 3: Dependency & Blast-Radius Graph (`@xyflow/react`)
- **Deterministic Layered Graph (`dagre` LR layout):** Strictly structured left-to-right in 4 columns (no chaotic force-directed physics):
  `Source Files` -> `Evidence (Quotes & Test Runs)` -> `Memory Claims` -> `Plan Tasks`
- **Custom Glass Nodes:** Compact monospace cards with status border accents and subtle backdrop blur.
- **Interactive "Blast Radius" Mode:** Clicking any `Source File` or `Stale Evidence` node dims unrelated nodes (`opacity-25`) and illuminates downstream edges with animated dashed strokes (`strokeDasharray`) showing every claim and task invalidated by changes to that file.

### View 4: Guardrail & Rejection Audit Log (Hallucination Firewall)
- **Chronological Table (`@tanstack/react-table`):**
  - Columns: `Time (HH:mm:ss.SSS)`, `Session`, `MCP Tool`, `Verdict` (`ACCEPTED` | `REJECTED`), `Rule / Reason Code` (`OCC_STALE_RESUME_PACKET`, `VERBATIM_QUOTE_NOT_FOUND`, `FILE_FINGERPRINT_MISMATCH`, `TEST_GATE_UNVERIFIED`).
- **Payload Diff Inspector (Right Drawer):** Clicking a rejected row shows the exact JSON arguments sent by the LLM side-by-side with the contract violation detail returned by Cairnmind.

---

## 6. Backend Integration & Directory Structure

### 6.1. Read-mostly HTTP + SSE Endpoints
Expose lightweight local endpoints on the Cairnmind server:
- `GET /api/ui/snapshot` — Initial state hydration (project metadata, `state_rev`, sessions, tasks, claims, graph edges, recent audit log).
- `GET /api/ui/file-context?path=...&lineStart=...&lineEnd=...` — Local read-only snippet for quote inspection (strictly path-sanitized to repo root).
- `GET /api/ui/events` — SSE stream emitting typed JSON events:
  - `state.updated` | `session.ack` | `evidence.stale` | `mcp.write.accepted` | `mcp.write.rejected`
- `POST /api/ui/actions/reverify` — Triggers on-demand fingerprint check.
- `POST /api/ui/actions/sessions/:id/drop` — Clears stale session state lock.

### 6.2. Directory Layout
```text
cairnmind/
└── ui/
    ├── index.html
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts          # Builds static assets to ui/dist/
    └── src/
        ├── api/                # REST client + typed SSE EventSource hook
        ├── types/              # Strict 1:1 TypeScript contracts matching backend
        ├── components/
        │   ├── glass/          # GlassPanel, GlassBadge, AmbientCanvas, StatusPulse
        │   ├── layout/         # AppShell, TopStatusBar, NavRail, InspectorDrawer, EventDock
        │   ├── matrix/         # SessionStrip, TaskGateTable
        │   ├── provenance/     # ClaimList, VerbatimCodeViewer, HashComparisonBar
        │   ├── graph/          # BlastRadiusGraph, GlassFlowNode, AnimatedStatusEdge
        │   └── audit/          # GuardrailAuditTable, McpPayloadInspector
        ├── hooks/              # useKeyboardNavigation (j/k/Esc), useLiveInvalidation
        └── App.tsx
```

---

## 7. Phased Implementation Plan (Execution Instructions for Coding Agent)

Execute strictly phase-by-phase. Every phase must pass `tsc --noEmit` and build cleanly before proceeding to the next.

- **Phase 1 — Backend Contracts & Local UI Endpoints:**
  Implement `/api/ui/snapshot`, `/api/ui/file-context` (with path traversal protection), `/api/ui/events` (SSE), and the two POST emergency actions. Export matching TypeScript interfaces in `ui/src/types/contracts.ts`. Include a deterministic mock dataset (`?mock=1`) so UI states (`VERIFIED`, `STALE`, `REJECTED`) can be tested visually at any time.
- **Phase 2 — Obsidian Glass Shell & Live Event Engine:**
  Scaffold Vite + React + TypeScript + Tailwind + `framer-motion`. Build reusable `glass/` primitives (`GlassPanel`, `GlassBadge`, `AmbientCanvas`), the viewport-locked `ResizablePanelGroup` shell, `TopStatusBar` with live SSE status indicator, collapsible `BottomEventDock`, and `Cmd+K` command palette shell.
- **Phase 3 — Matrix, Provenance & Guardrail Views:**
  Build View 1 (`Plan & Concurrency Matrix`), View 2 (`Provenance Inspector` with highlighted verbatim quotes and SHA-256 comparison), and View 4 (`Guardrail Audit Log` with JSON payload inspector). Wire up real-time SSE cache updates and status flash animations.
- **Phase 4 — Blast-Radius Graph & CLI Static Hosting:**
  Implement View 3 (`@xyflow/react` + `dagre` layered graph with interactive Blast Radius highlighting and animated edges), add `j`/`k`/`Esc` keyboard navigation, and wire the Cairnmind CLI (`cairnmind ui`) to serve the compiled `ui/dist` static bundle on localhost.
