"""天地课堂联播台领域模型。

约定见 docs/domain.md：
- 业务身份由调用方提供稳定标识；
- 事件时间与接收时间分离；
- 审计与事件日志只追加、不覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Role(str, Enum):
    CONTROL = "control"        # 活动总控
    SCHOOL = "school"          # 学校（港澳校区）侧
    AUDITOR = "auditor"        # 审计员


class PlanStatus(str, Enum):
    SUBMITTED = "SUBMITTED"                # 学校已上报
    SCHOOL_APPROVED = "SCHOOL_APPROVED"    # 校内审核链通过
    CONTROL_APPROVED = "CONTROL_APPROVED"  # 总控审核通过，可占位
    LOCKED = "LOCKED"                      # 已随场次锁定
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class HoldStatus(str, Enum):
    HELD = "HELD"
    RELEASED = "RELEASED"


class SessionStatus(str, Enum):
    LOCKED = "LOCKED"
    CANCELLED = "CANCELLED"


class ConflictKind(str, Enum):
    WINDOW_SHORTENED = "WINDOW_SHORTENED"    # 空间站直播窗口缩短
    SIMULTANEOUS_EDIT = "SIMULTANEOUS_EDIT"  # 两地同时改动


class ConflictStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class NoticeStatus(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    SUPPRESSED = "SUPPRESSED"


class ApiError(Exception):
    """携带 HTTP 状态码与机器可读错误码的领域错误。"""

    def __init__(self, status: int, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}

    def to_body(self) -> dict[str, Any]:
        body = {"error": self.code, "message": self.message}
        body.update(self.extra)
        return body


@dataclass
class School:
    id: str
    name: str
    region: str  # HK / MO


@dataclass
class Campus:
    id: str
    school_id: str
    venue_capacity: int


@dataclass
class Window:
    """空间站过境直播窗口：时间区间 + 信号链路可承载席位。"""
    id: str
    campus_id: str
    start: str
    end: str
    signal_capacity: int


@dataclass
class Student:
    """未成年学生资料，属敏感个人信息。"""
    id: str
    school_id: str
    name: str
    grade: str
    contact: str


@dataclass
class Question:
    id: str
    text: str
    student_id: Optional[str] = None


@dataclass
class Plan:
    """学校参与计划：提问征集、课后材料随计划一起归档。"""
    id: str
    school_id: str
    window_id: str
    seats: int
    student_ids: list[str]
    questions: list[Question]
    materials: list[str]
    status: PlanStatus
    approvals: list[dict] = field(default_factory=list)
    version: int = 0


@dataclass
class Hold:
    id: str
    plan_id: str
    window_id: str
    school_id: str
    seats: int
    status: HoldStatus


@dataclass
class Allocation:
    school_id: str
    seats: int
    plan_ids: list[str]


@dataclass
class Session:
    id: str
    window_id: str
    campus_id: str
    start: str
    end: str
    allocations: list[Allocation]
    status: SessionStatus


@dataclass
class ScheduleVersion:
    """不可变的排期版本快照，重启后通过事件重放完整还原。"""
    version: int
    reason: str
    at: str
    sessions: list[Session]


@dataclass
class ConflictRecord:
    id: str
    kind: ConflictKind
    status: ConflictStatus
    detected_reasons: list[str]
    base_version: int
    current_version: int
    actor_side: str
    detail: dict
    options: list[dict]
    suggested: Optional[str]
    resolution: Optional[str] = None
    scope: dict = field(default_factory=dict)


@dataclass
class Notice:
    """通知草稿箱条目；派发前按最新状态复核，失效即抑制。"""
    id: str
    kind: str
    status: NoticeStatus
    basis_version: int
    conflict_id: Optional[str]
    scope_plan_ids: list[str]
    audience: str  # participants / control
    suppressed_reason: Optional[str] = None
    recipients: Optional[list[str]] = None
    dispatched_at: Optional[str] = None
