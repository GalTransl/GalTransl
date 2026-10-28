import type { AgentContextUsage } from '../../lib/api';

export type Session = {
  id: string; title: string; game_dir: string; workspace: string; status: string;
  created_at: number; updated_at: number; seq: number; model_name: string;
  profile_name: string; permission_mode: string; error: string;
};
export type AgentEvent = {
  images?: import('../agent/images').AgentImage[];
  type: string; seq?: number; text?: string; messageId?: string; toolCallId?: string;
  toolName?: string; arguments?: Record<string, unknown>; isError?: boolean;
  replace?: boolean;
  requestId?: string; question?: string; status?: string; message?: string;
  questions?: { question: string; options: string[]; recommended?: string; multiSelect?: boolean }[];
  permission_mode?: string;
  input?: number; output?: number; total?: number; active?: boolean; partial?: boolean;
  used_tokens?: number | null; window_tokens?: number; stopReason?: string; cache?: AgentContextUsage['cache'];
};
export type Snapshot = {
  session: Session; events: AgentEvent[]; live_events: AgentEvent[]; cursor: number;
  pending: AgentEvent | null; has_more: boolean; before_seq: number | null;
};
export type SkillStatus = {
  installed: boolean; current_commit: string; latest_commit: string; update_available: boolean;
};
export type Environment = {
  ready: boolean; problems: string[]; agent_engine: string; python_version: string;
  settings: { skill_dir: string; python_path: string };
  skill: SkillStatus;
};
export type SessionList = { sessions: Session[]; next_offset: number | null; total: number; running_game_dirs?: string[] };
export type FileList = { path: string; entries: { name: string; is_dir: boolean; size: number }[]; next_offset: number | null; total: number };
export type Preview = { path: string; content: string; binary: boolean; size: number; next_offset: number | null };
export const isRunning = (session?: Session | null) => Boolean(session && ['starting', 'running', 'awaiting_permission', 'awaiting_input', 'stopping'].includes(session.status));
