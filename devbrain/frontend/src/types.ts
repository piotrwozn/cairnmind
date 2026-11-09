export type Json = Record<string, unknown>;
export interface Project {
  id: string;
  name: string;
  root: string;
  roots?: string[];
  memory_snapshot?: string | null | boolean;
}
export interface Profile {
  id: string;
  client: 'codex' | 'claude';
  label: string;
  model?: string | null;
}
export interface Slot {
  id: string;
  profile_id: string;
}
export interface Settings {
  scan_roots: string[];
  default_slots: Slot[];
  project_slots: Record<string, Slot[]>;
  last_project: string | null;
}
export interface ClientRun extends Json {
  id: string;
  run_id?: string;
  generation: number;
  project_id?: string;
  worktree?: string;
  client: string;
  slot_id?: string;
  lifecycle?: string;
  state?: string;
  session_id?: string;
  exit_code?: number | null;
  memory_readiness?: Json;
  purpose?: string;
}
export interface CockpitState {
  projects: Project[];
  profiles: Profile[];
  settings: Settings;
  runs: ClientRun[];
  capabilities?: Json;
}
export interface RecordItem extends Json {
  id: string;
  kind: string;
  content: string;
  status: string;
  version: number;
  metadata?: Json;
  source_ids?: string[];
}
export interface SourceItem extends Json {
  id: string;
  uri?: string;
  kind?: string;
  content?: string;
  sha256?: string;
  metadata?: Json;
}
export interface GraphNode extends Json {
  id: string;
  kind: string;
  label?: string;
  project_id?: string;
  source_id?: string;
  metadata?: Json;
}
export interface GraphEdge extends Json {
  id?: string;
  from_id: string;
  to_id: string;
  kind?: string;
}
export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  groups?: Json[];
  counts?: Json;
  truncated?: boolean;
  cursor?: string;
  next_cursor?: string | null;
}
export const defaultSettings: Settings = {
  scan_roots: [],
  default_slots: [
    { id: 'codex', profile_id: 'codex' },
    { id: 'claude', profile_id: 'claude' },
  ],
  project_slots: {},
  last_project: null,
};
