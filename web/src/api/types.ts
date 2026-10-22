export type {
  AuthState,
  BuddySettings,
  CandidateItem,
  CompletionReward,
  GoalItem,
  MemoItem,
  Player,
  QuickPreviewResult,
  SplitDraftItem,
  TaskItem,
  TodayPayload,
  UnsortedItem,
} from "../api";

export interface ReportItem {
  id: number;
  task_id?: number | null;
  goal_id?: number | null;
  title?: string;
  summary?: string;
  body?: string;
  created_at?: string;
  status?: string;
  task_title?: string;
  task_text?: string;
  goal_title?: string;
  suggested_status?: "progress" | "done" | "blocked";
  details?: string;
}

export interface Achievement {
  code: string;
  title: string;
  description?: string;
  unlocked?: boolean;
  unlocked_at?: string | null;
}

export interface RewardItem {
  id: number;
  title: string;
  cost: number;
  active?: boolean;
}

export interface MapSummary {
  id: number;
  goal_id?: number;
  title: string;
  progress?: number;
  progress_percent?: number;
  current_stage?: number;
  current_stage_number?: number;
  stage_count?: number;
  stale_days?: number | null;
}

export interface SeedItem {
  id: number;
  title: string;
  body?: string;
  raw_text?: string;
  created_at?: string;
}

export interface SeedPlan {
  stages: Array<{ title: string; why: string; target_date: string | null }>;
  first_quests: Array<{
    title: string;
    why: string;
    due_hint: string;
    estimate_minutes: number | null;
  }>;
  ai_used: boolean;
  why: string;
  done_when: string;
  target_date: string | null;
}
