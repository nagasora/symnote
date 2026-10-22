"""SymNote Web API のリクエスト・レスポンス型定義（Pydantic v2）。"""

from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, StrictInt

TaskStatus = Literal["inbox", "today", "week", "done"]
ActiveTaskStatus = Literal["inbox", "today", "week"]
EffortLevel = Literal["short", "medium", "long"]
EnergyLevel = Literal["low", "mid", "high"]
RecurrenceFrequency = Literal["daily", "weekly", "weekdays", "monthly", "custom"]
CandidateStatus = Literal["pending", "approved", "rejected"]
CaptureKind = Literal["auto", "task", "memo", "unsorted"]


class RecurrenceSpec(BaseModel):
    frequency: RecurrenceFrequency
    interval: int = Field(default=1, ge=1, le=365)
    weekdays: List[int] = Field(default_factory=list)
    month_day: Optional[int] = Field(default=None, ge=1, le=31)
    end_date: Optional[str] = None


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=4000)
    status: ActiveTaskStatus = "today"
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    importance: int = Field(default=3, ge=1, le=5)
    urgency: int = Field(default=3, ge=1, le=5)
    effort: Optional[EffortLevel] = None
    energy: Optional[EnergyLevel] = None
    goal_id: Optional[int] = None
    estimate_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    recurrence: Optional[RecurrenceSpec] = None


class TaskPatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    details: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[ActiveTaskStatus] = None
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    importance: Optional[int] = Field(default=None, ge=1, le=5)
    urgency: Optional[int] = Field(default=None, ge=1, le=5)
    effort: Optional[EffortLevel] = None
    energy: Optional[EnergyLevel] = None
    goal_id: Optional[int] = None
    estimate_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    recurrence: Optional[RecurrenceSpec] = None
    stop_recurrence: bool = False


class TaskReopenRequest(BaseModel):
    status: ActiveTaskStatus = "today"
    discard_next_task_id: Optional[int] = None


class MemoCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    tags: str = Field(default="", max_length=200)
    date: Optional[str] = None


class MemoPatchRequest(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=10000)
    tags: Optional[str] = Field(default=None, max_length=200)


class CandidateApproveRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    details: Optional[str] = Field(default=None, max_length=2000)
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    status: ActiveTaskStatus = "today"


class UnsortedSortRequest(BaseModel):
    as_kind: Literal["task", "memo"]
    title: Optional[str] = Field(default=None, max_length=200)
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    status: ActiveTaskStatus = "today"


class CapturePreviewRequest(BaseModel):
    text: str = Field(default="", max_length=4000)


class CaptureCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: CaptureKind = "auto"
    status: ActiveTaskStatus = "today"
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    goal_id: Optional[int] = None
    estimate_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


class BrainDumpSplitRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class BrainDumpTaskDraft(BaseModel):
    kind: Literal["task", "seed", "memo"] = "task"
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=2000)
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    estimate_minutes: Optional[int] = Field(default=15, ge=1, le=1440)
    importance: int = Field(default=3, ge=1, le=5)
    urgency: int = Field(default=3, ge=1, le=5)
    status: ActiveTaskStatus = "today"
    goal_id: Optional[int] = None


class BrainDumpCommitRequest(BaseModel):
    items: List[BrainDumpTaskDraft] = Field(min_length=1, max_length=30)
    source_text: str = Field(default="", max_length=10000)
    save_source_memo: bool = False


class GoalCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    why: str = Field(default="", max_length=2000)
    success_criteria: str = Field(default="", max_length=2000)
    target_date: Optional[str] = None
    parent_id: Optional[int] = None


class GoalPatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    why: Optional[str] = Field(default=None, max_length=2000)
    success_criteria: Optional[str] = Field(default=None, max_length=2000)
    target_date: Optional[str] = None
    status: Optional[Literal["active", "done", "archived"]] = None
    parent_id: Optional[int] = None


class GoalBreakdownSubgoalRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    why: str = Field(default="", max_length=2000)


class GoalBreakdownTaskRequest(GoalBreakdownSubgoalRequest):
    due_hint: Literal["", "今日", "今週", "来週", "今月"] = ""
    estimate_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


class GoalBreakdownCommitRequest(BaseModel):
    subgoals: List[GoalBreakdownSubgoalRequest] = Field(default_factory=list, max_length=10)
    tasks: List[GoalBreakdownTaskRequest] = Field(default_factory=list, max_length=30)
    selected_subgoals: List[StrictInt] = Field(default_factory=list, max_length=10)
    selected_tasks: List[StrictInt] = Field(default_factory=list, max_length=30)


class PasscodeLoginRequest(BaseModel):
    passcode: str = Field(default="", max_length=200)


class E2ELoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(default="", max_length=100)
    is_owner: bool = False


class OnboardingRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    seed_sample_tasks: bool = False


class InviteCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    note: str = Field(default="", max_length=200)
