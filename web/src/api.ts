import { isNativeApp, nativeRequest } from "./nativeClient";

export interface UserInfo {
  id: number;
  email: string;
  name: string;
  is_owner: boolean;
  onboarded: boolean;
}

export interface AuthState {
  authenticated: boolean;
  mode: string;
  google_enabled: boolean;
  mobile_enabled?: boolean;
  passcode_enabled: boolean;
  e2e_enabled: boolean;
  user: UserInfo | null;
}

export interface RecurrenceRuleDTO {
  frequency: "daily" | "weekly" | "weekdays" | "monthly";
  interval: number;
  weekdays: number[];
  month_day: number | null;
  end_date: string | null;
  label: string;
}

export interface TaskItem {
  id: number;
  title: string;
  details: string;
  raw_text: string;
  status: "inbox" | "today" | "week" | "done";
  due_date: string | null;
  due_time: string | null;
  importance: number;
  urgency: number;
  goal_id: number | null;
  goal_title: string | null;
  estimate_minutes: number | null;
  actual_minutes: number | null;
  completed_at: string | null;
  recurrence: RecurrenceRuleDTO | null;
  source: { type: string; excerpt?: string | null; source_ref?: string | null } | null;
  created_at: string;
}

export interface MemoItem {
  id: number;
  title: string;
  body: string;
  raw_text: string;
  tags: string;
  created_at: string;
}

export interface CandidateItem {
  id: number;
  source_type: string;
  source_ref: string;
  title: string;
  details: string;
  due_date: string | null;
  due_time: string | null;
  excerpt: string;
  confidence: number;
  status: "pending" | "approved" | "rejected";
  task_id: number | null;
  created_at: string;
}

export interface UnsortedItem {
  id: number;
  title: string;
  raw_text: string;
  created_at: string;
}

export interface GoalItem {
  id: number;
  title: string;
  why: string;
  target_date: string | null;
  status: "active" | "done" | "archived";
  parent_id: number | null;
  depth: number;
  open_tasks: number;
  done_tasks: number;
  progress: number;
  progress_percent: number;
  last_activity: string | null;
  stale_days: number | null;
  next_task: TaskItem | null;
}

export interface GoalContext {
  id: number;
  title: string;
  why: string;
  success_criteria: string;
  target_date: string | null;
  status: "active" | "done" | "archived";
  parent_id: number | null;
  ancestors: Array<{
    id: number;
    title: string;
    parent_id: number | null;
  }>;
  path: string[];
  subgoals: Array<{
    id: number;
    title: string;
    why: string;
    status: "active" | "done" | "archived";
  }>;
  open_tasks: TaskItem[];
  done_tasks: TaskItem[];
}

export interface GoalBreakdownSubgoal {
  title: string;
  why: string;
}

export interface GoalBreakdownTask {
  title: string;
  why: string;
  due_hint: "" | "今日" | "今週" | "来週" | "今月";
  estimate_minutes: number | null;
}

export interface GoalBreakdownPreview {
  subgoals: GoalBreakdownSubgoal[];
  tasks: GoalBreakdownTask[];
  ai_used: boolean;
}

export interface NextActionSuggestion {
  task_id: number;
  title: string;
  reason: string;
  estimate_minutes: number | null;
  due_date: string | null;
  due_time: string | null;
}

export interface TodayPayload {
  today: string;
  weekday_ja: string;
  overview: {
    open_tasks: number;
    overdue_tasks: number;
    due_today_tasks: number;
    today_focus_tasks: number;
    completed_tasks: number;
  };
  next_action: NextActionSuggestion | null;
  overdue: TaskItem[];
  due_today: TaskItem[];
  completed_today: TaskItem[];
  calendar_events: Array<{
    id: string;
    summary: string;
    start: string;
    end: string;
    is_all_day: boolean;
    html_link?: string | null;
  }>;
  pending_candidates_count: number;
  unsorted_count: number;
  inbox_badge_count: number;
}

export interface SplitDraftItem {
  title: string;
  details: string;
  due_date: string | null;
  due_time: string | null;
  status: "inbox" | "today" | "week";
  importance: number;
  urgency: number;
  estimate_minutes: number | null;
  goal_id?: number | null;
}

export interface QuickPreviewResult {
  clean_title: string;
  due_date: string | null;
  due_time: string | null;
  recurrence: {
    frequency: "daily" | "weekly" | "weekdays" | "monthly";
    interval: number;
    weekdays: number[];
    month_day: number | null;
    label: string;
  } | null;
  kind: "task" | "memo" | "unsorted";
  chips: Array<{ kind: string; label: string }>;
}

export async function apiFetch<T>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const method = options.method ?? (options.body !== undefined ? "POST" : "GET");
  if (isNativeApp()) {
    const response = await nativeRequest(path, method, options.body);
    if (response.status < 200 || response.status >= 300) {
      const data = response.data as { error?: string | { message?: string } } | null;
      const message = typeof data?.error === "string" ? data.error : data?.error?.message;
      throw new Error(message || `通信エラーが発生しました (${response.status})`);
    }
    return response.data as T;
  }
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-SymNote": "1",
  };
  let bodyStr: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    bodyStr = JSON.stringify(options.body);
  }
  const res = await fetch(path, {
    method,
    headers,
    body: bodyStr,
    credentials: "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg =
      (data && typeof data === "object" && "error" in data && typeof data.error === "string"
        ? data.error
        : null) || `通信エラーが発生しました (${res.status})`;
    throw new Error(msg);
  }
  return data as T;
}
