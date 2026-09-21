"""天地课堂联播台应用服务。

所有写操作在同一把锁内完成“校验 → 追加事件 → 投影更新”，保证并发占位
不超额；投影完全由事件重放得到，重启可还原全部排期版本。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

from .eventstore import EventStore, utc_now
from .models import (
    ApiError,
    Allocation,
    Campus,
    ConflictKind,
    ConflictRecord,
    ConflictStatus,
    Hold,
    HoldStatus,
    Notice,
    NoticeStatus,
    Plan,
    PlanStatus,
    Question,
    ScheduleVersion,
    School,
    Session,
    SessionStatus,
    Student,
    Window,
)


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    school_id: Optional[str] = None


# ---------------------------------------------------------------- 序列化

def _question_to_dict(q: Question) -> dict:
    return {"id": q.id, "text": q.text, "student_id": q.student_id}


def _question_from_dict(d: dict) -> Question:
    return Question(id=d["id"], text=d["text"], student_id=d.get("student_id"))


def _allocation_to_dict(a: Allocation) -> dict:
    return {"school_id": a.school_id, "seats": a.seats, "plan_ids": list(a.plan_ids)}


def _allocation_from_dict(d: dict) -> Allocation:
    return Allocation(school_id=d["school_id"], seats=d["seats"], plan_ids=list(d["plan_ids"]))


def _session_to_dict(s: Session) -> dict:
    return {
        "id": s.id,
        "window_id": s.window_id,
        "campus_id": s.campus_id,
        "start": s.start,
        "end": s.end,
        "status": s.status.value if isinstance(s.status, SessionStatus) else s.status,
        "allocations": [_allocation_to_dict(a) for a in s.allocations],
    }


def _session_from_dict(d: dict) -> Session:
    return Session(
        id=d["id"],
        window_id=d["window_id"],
        campus_id=d["campus_id"],
        start=d["start"],
        end=d["end"],
        status=SessionStatus(d["status"]),
        allocations=[_allocation_from_dict(a) for a in d["allocations"]],
    )


# ---------------------------------------------------------------- 主服务

class ApplicationService:
    def __init__(self, store: EventStore):
        self.store = store
        self._lock = threading.RLock()
        # 投影
        self.schools: dict[str, School] = {}
        self.campuses: dict[str, Campus] = {}
        self.windows: dict[str, Window] = {}
        self.students: dict[str, Student] = {}
        self.plans: dict[str, Plan] = {}
        self.holds: dict[str, Hold] = {}
        self.current_sessions: dict[str, Session] = {}
        self.versions: list[ScheduleVersion] = []
        self.conflicts: dict[str, ConflictRecord] = {}
        self.notices: dict[str, Notice] = {}
        self.audit: list[dict] = []
        self._conflict_seq = 0
        self._notice_seq = 0
        self.store.replay(self._apply)

    # ---------------------------------------------------------- 事件投影
    def _emit(self, event_type: str, payload: dict, **kw) -> dict:
        event = self.store.append(event_type, payload, **kw)
        self._apply(event)
        return event

    def _apply(self, event: dict) -> None:
        etype = event["type"]
        p = event["payload"]
        if etype == "SCHOOL_REGISTERED":
            self.schools[p["id"]] = School(**p)
        elif etype == "CAMPUS_REGISTERED":
            self.campuses[p["id"]] = Campus(**p)
        elif etype == "WINDOW_ADDED" or etype == "WINDOW_REVISED":
            self.windows[p["id"]] = Window(
                id=p["id"], campus_id=p["campus_id"], start=p["start"],
                end=p["end"], signal_capacity=p["signal_capacity"],
            )
        elif etype == "STUDENT_ADDED":
            self.students[p["id"]] = Student(**p)
        elif etype == "PLAN_SUBMITTED":
            self.plans[p["id"]] = Plan(
                id=p["id"], school_id=p["school_id"], window_id=p["window_id"],
                seats=p["seats"], student_ids=list(p["student_ids"]),
                questions=[_question_from_dict(q) for q in p["questions"]],
                materials=list(p["materials"]), status=PlanStatus.SUBMITTED,
                approvals=[], version=1,
            )
        elif etype == "PLAN_APPROVAL":
            plan = self.plans[p["plan_id"]]
            plan.status = PlanStatus(p["status"])
            plan.approvals.append(p["approval"])
            plan.version += 1
        elif etype == "PLAN_STATUS_CHANGED":
            plan = self.plans[p["plan_id"]]
            plan.status = PlanStatus(p["status"])
            plan.version += 1
        elif etype == "HOLD_HELD":
            self.holds[p["id"]] = Hold(
                id=p["id"], plan_id=p["plan_id"], window_id=p["window_id"],
                school_id=p["school_id"], seats=p["seats"], status=HoldStatus.HELD,
            )
        elif etype == "HOLD_RELEASED":
            if p["id"] in self.holds:
                self.holds[p["id"]].status = HoldStatus.RELEASED
        elif etype == "SCHEDULE_VERSION_BUMPED":
            sessions = [_session_from_dict(s) for s in p["sessions"]]
            self.versions.append(ScheduleVersion(
                version=p["version"], reason=p["reason"], at=event["event_time"],
                sessions=sessions,
            ))
            self.current_sessions = {s.window_id: s for s in sessions}
        elif etype == "CONFLICT_DETECTED":
            self._conflict_seq += 1
            self.conflicts[p["id"]] = ConflictRecord(
                id=p["id"], kind=ConflictKind(p["kind"]),
                status=ConflictStatus.OPEN,
                detected_reasons=list(p["detected_reasons"]),
                base_version=p["base_version"], current_version=p["current_version"],
                actor_side=p["actor_side"], detail=dict(p["detail"]),
                options=list(p["options"]), suggested=p.get("suggested"),
            )
        elif etype == "CONFLICT_RESOLVED":
            conflict = self.conflicts[p["id"]]
            conflict.status = ConflictStatus.RESOLVED
            conflict.resolution = p["choice"]
        elif etype == "NOTICE_DRAFTED":
            self._notice_seq += 1
            self.notices[p["id"]] = Notice(
                id=p["id"], kind=p["kind"], status=NoticeStatus.PENDING,
                basis_version=p["basis_version"], conflict_id=p.get("conflict_id"),
                scope_plan_ids=list(p["scope_plan_ids"]), audience=p["audience"],
            )
        elif etype == "NOTICE_SUPPRESSED":
            self.notices[p["id"]].status = NoticeStatus.SUPPRESSED
            self.notices[p["id"]].suppressed_reason = p["reason"]
        elif etype == "NOTICE_DISPATCHED":
            notice = self.notices[p["id"]]
            notice.status = NoticeStatus.DISPATCHED
            notice.recipients = list(p["recipients"])
            notice.dispatched_at = event["event_time"]
        elif etype == "AUDIT_ENTRY":
            self.audit.append(dict(p))

    # ---------------------------------------------------------- 通用辅助
    def _audit(self, actor: Actor, action: str, resource: str, decision: str, **detail) -> None:
        self._emit("AUDIT_ENTRY", {
            "at": utc_now(),
            "actor_id": actor.id, "role": actor.role,
            "school_id": actor.school_id, "action": action,
            "resource": resource, "decision": decision, "detail": detail,
        })

    def _require_role(self, actor: Actor, roles: set[str], action: str, resource: str):
        if actor.role not in roles:
            self._audit(actor, action, resource, "DENIED", reason="role_forbidden")
            raise ApiError(403, "FORBIDDEN", f"角色 {actor.role} 无权执行 {action}")

    def _require_school_scope(self, actor: Actor, school_id: str, action: str, resource: str):
        if actor.role == "school" and actor.school_id != school_id:
            self._audit(actor, action, resource, "DENIED",
                        reason="cross_school", target_school=school_id)
            raise ApiError(403, "FORBIDDEN", "禁止访问其他学校的数据")

    def _get_plan_checked(self, actor: Actor, plan_id: str) -> Plan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise ApiError(404, "PLAN_NOT_FOUND", f"计划 {plan_id} 不存在")
        self._require_school_scope(actor, plan.school_id, "read_plan", plan_id)
        return plan

    @property
    def current_version(self) -> int:
        return len(self.versions)

    def _window_capacity(self, window: Window) -> int:
        campus = self.campuses[window.campus_id]
        return min(window.signal_capacity, campus.venue_capacity)

    def _active_hold_ids(self, window_id: str) -> list[str]:
        """窗口内当前占座的 hold：hold 仍有效且计划未退出/驳回。"""
        result = []
        for hold in self.holds.values():
            if hold.window_id != window_id or hold.status != HoldStatus.HELD:
                continue
            plan = self.plans[hold.plan_id]
            if plan.status in (PlanStatus.WITHDRAWN, PlanStatus.REJECTED):
                continue
            result.append(hold.id)
        return result

    def _used_seats(self, window_id: str) -> int:
        return sum(self.holds[i].seats for i in self._active_hold_ids(window_id))

    def _build_session(self, window: Window) -> Session:
        hold_ids = sorted(self._active_hold_ids(window.id),
                          key=lambda hid: self.holds[hid].plan_id)
        allocations: dict[str, Allocation] = {}
        for hid in hold_ids:
            hold = self.holds[hid]
            alloc = allocations.setdefault(hold.school_id,
                                           Allocation(hold.school_id, 0, []))
            alloc.seats += hold.seats
            alloc.plan_ids.append(hold.plan_id)
        return Session(
            id=f"session-{window.id}", window_id=window.id,
            campus_id=window.campus_id, start=window.start, end=window.end,
            status=SessionStatus.LOCKED,
            allocations=sorted(allocations.values(), key=lambda a: a.school_id),
        )

    def _snapshot_payload(self, reason: str) -> dict:
        return {
            "version": self.current_version + 1,
            "reason": reason,
            "sessions": [_session_to_dict(s) for s in
                         sorted(self.current_sessions.values(), key=lambda s: s.window_id)],
        }

    def _plan_in_session(self, plan_id: str) -> Optional[Session]:
        for session in self.current_sessions.values():
            for alloc in session.allocations:
                if plan_id in alloc.plan_ids:
                    return session
        return None

    def _draft_notice(self, plan_id: str, kind: str, conflict_id: Optional[str]) -> str:
        notice_id = f"notice-{self._notice_seq + 1}"
        self._emit("NOTICE_DRAFTED", {
            "id": notice_id, "kind": kind, "basis_version": self.current_version,
            "conflict_id": conflict_id, "scope_plan_ids": [plan_id],
            "audience": "participants",
        })
        return notice_id

    def _draft_control_notice(self, kind: str, plan_ids: list[str],
                              conflict_id: Optional[str]) -> str:
        notice_id = f"notice-{self._notice_seq + 1}"
        self._emit("NOTICE_DRAFTED", {
            "id": notice_id, "kind": kind, "basis_version": self.current_version,
            "conflict_id": conflict_id, "scope_plan_ids": list(plan_ids),
            "audience": "control",
        })
        return notice_id

    def _check_version(self, actor: Actor, expected: Optional[int], side: str,
                       detail_extra: dict) -> None:
        if expected is None:
            raise ApiError(400, "VERSION_REQUIRED", "该操作必须携带 expected_version")
        if expected == self.current_version:
            return
        recent = [{"version": v.version, "reason": v.reason}
                  for v in self.versions[expected:]]
        conflict_id = f"conflict-{self._conflict_seq + 1}"
        reasons = [
            f"操作基于排期版本 v{expected}，但总控当前版本已到 v{self.current_version}",
            "期间发生的变更：" + "；".join(
                f"v{c['version']} {c['reason']}" for c in recent) if recent else "",
        ]
        self._emit("CONFLICT_DETECTED", {
            "id": conflict_id, "kind": ConflictKind.SIMULTANEOUS_EDIT.value,
            "detected_reasons": [r for r in reasons if r],
            "base_version": expected, "current_version": self.current_version,
            "actor_side": side,
            "detail": {**detail_extra, "concurrent_changes": recent},
            "options": [
                {"choice": "RETRY", "label": "基于最新版本重试",
                 "description": "拉取 v%d 最新排期后重新提交，系统按最新状态重算" % self.current_version},
            ],
            "suggested": "RETRY",
        })
        self._draft_control_notice("SIMULTANEOUS_EDIT",
                                   detail_extra.get("plan_ids", []), conflict_id)
        self._audit(actor, "simultaneous_edit", f"conflict:{conflict_id}", "CONFLICT",
                    base=expected, current=self.current_version)
        raise ApiError(409, "SIMULTANEOUS_EDIT",
                       "两地同时改动：请先同步最新排期版本再重试",
                       {"conflict_id": conflict_id,
                        "current_version": self.current_version})

    # ---------------------------------------------------------- 基础数据
    def register_school(self, actor: Actor, school_id: str, name: str, region: str) -> School:
        with self._lock:
            self._require_role(actor, {"control"}, "register_school", school_id)
            if school_id in self.schools:
                return self.schools[school_id]
            if region not in ("HK", "MO"):
                raise ApiError(400, "BAD_REGION", "region 必须为 HK 或 MO")
            self._emit("SCHOOL_REGISTERED",
                       {"id": school_id, "name": name, "region": region})
            self._audit(actor, "register_school", school_id, "ALLOWED")
            return self.schools[school_id]

    def register_campus(self, actor: Actor, campus_id: str, school_id: str,
                        venue_capacity: int) -> Campus:
        with self._lock:
            self._require_role(actor, {"control"}, "register_campus", campus_id)
            if campus_id in self.campuses:
                return self.campuses[campus_id]
            if venue_capacity <= 0:
                raise ApiError(400, "BAD_CAPACITY", "容量必须为正整数")
            self._emit("CAMPUS_REGISTERED", {
                "id": campus_id, "school_id": school_id,
                "venue_capacity": venue_capacity})
            self._audit(actor, "register_campus", campus_id, "ALLOWED")
            return self.campuses[campus_id]

    def add_window(self, actor: Actor, window_id: str, campus_id: str, start: str,
                   end: str, signal_capacity: int) -> Window:
        with self._lock:
            self._require_role(actor, {"control"}, "add_window", window_id)
            if window_id in self.windows:
                raise ApiError(409, "WINDOW_EXISTS", f"窗口 {window_id} 已存在")
            if campus_id not in self.campuses:
                raise ApiError(404, "CAMPUS_NOT_FOUND", "校区不存在")
            if signal_capacity <= 0:
                raise ApiError(400, "BAD_CAPACITY", "信号容量必须为正整数")
            self._emit("WINDOW_ADDED", {
                "id": window_id, "campus_id": campus_id, "start": start,
                "end": end, "signal_capacity": signal_capacity})
            self._audit(actor, "add_window", window_id, "ALLOWED")
            return self.windows[window_id]

    def add_student(self, actor: Actor, student: Student) -> Student:
        with self._lock:
            self._require_role(actor, {"school"}, "add_student", student.id)
            self._require_school_scope(actor, student.school_id, "add_student", student.id)
            if student.id in self.students:
                return self.students[student.id]
            if student.school_id not in self.schools:
                raise ApiError(404, "SCHOOL_NOT_FOUND", "学校未注册")
            self._emit("STUDENT_ADDED", student.__dict__.copy())
            self._audit(actor, "add_student", student.id, "ALLOWED")
            return student

    def list_students(self, actor: Actor, school_id: str) -> list[Student]:
        with self._lock:
            # 未成年人资料按角色隔离：仅所属学校可访问完整资料。
            if actor.role != "school" or actor.school_id != school_id:
                self._audit(actor, "list_students", f"school:{school_id}", "DENIED",
                            reason="pii_role_isolation")
                raise ApiError(403, "FORBIDDEN", "未成年人资料仅所属学校可访问")
            self._audit(actor, "list_students", f"school:{school_id}", "ALLOWED",
                        pii_access=True)
            return [s for s in self.students.values() if s.school_id == school_id]

    # ---------------------------------------------------------- 参与计划
    def submit_plan(self, actor: Actor, plan_id: str, window_id: str, seats: int,
                    student_ids: list[str], questions: list[dict],
                    materials: list[str], idempotency_key: str,
                    event_time: Optional[str] = None) -> Plan:
        with self._lock:
            self._require_role(actor, {"school"}, "submit_plan", plan_id)
            dedupe = f"plan-submit:{actor.school_id}:{idempotency_key}"
            existing = next((e for e in self.store.all_events
                             if e.get("dedupe_key") == dedupe), None)
            if existing:
                # 重复上报：原样返回，不产生新事件、不重复占位。
                return self.plans[existing["payload"]["id"]]
            if plan_id in self.plans:
                raise ApiError(409, "PLAN_EXISTS", f"计划 {plan_id} 已存在")
            window = self.windows.get(window_id)
            if window is None:
                raise ApiError(404, "WINDOW_NOT_FOUND", "直播窗口不存在")
            if seats <= 0 or seats != len(student_ids):
                raise ApiError(400, "BAD_SEATS", "席位必须为正整数且与学生名单人数一致")
            for sid in student_ids:
                student = self.students.get(sid)
                if student is None or student.school_id != actor.school_id:
                    raise ApiError(400, "STUDENT_INVALID", f"学生 {sid} 不属于本校")
            for q in questions:
                if q.get("student_id"):
                    student = self.students.get(q["student_id"])
                    if student is None or student.school_id != actor.school_id:
                        raise ApiError(400, "QUESTION_INVALID", "提问人与学校不匹配")
            payload = {
                "id": plan_id, "school_id": actor.school_id, "window_id": window_id,
                "seats": seats, "student_ids": list(student_ids),
                "questions": [{"id": q["id"], "text": q["text"],
                               "student_id": q.get("student_id")} for q in questions],
                "materials": list(materials),
            }
            self._emit("PLAN_SUBMITTED", payload, event_id=f"evt-plan-{plan_id}",
                       event_time=event_time, dedupe_key=dedupe)
            self._audit(actor, "submit_plan", plan_id, "ALLOWED", seats=seats)
            return self.plans[plan_id]

    def approve_or_reject(self, actor: Actor, plan_id: str, action: str,
                          note: str) -> Plan:
        with self._lock:
            plan = self._get_plan_checked(actor, plan_id)
            if plan.status in (PlanStatus.LOCKED, PlanStatus.WITHDRAWN, PlanStatus.REJECTED):
                raise ApiError(409, "PLAN_NOT_ACTIONABLE",
                               f"计划当前状态 {plan.status.value} 不可审核")
            approval = {"stage": "school" if actor.role == "school" else "control",
                        "action": action, "actor_id": actor.id, "note": note}
            # 幂等：同一审核人同一环节重复提交，直接返回当前状态（先于阶段校验）。
            if any(a["stage"] == approval["stage"] and a["action"] == action
                   and a["actor_id"] == actor.id for a in plan.approvals):
                return plan
            if actor.role == "school":
                if plan.status != PlanStatus.SUBMITTED:
                    raise ApiError(409, "APPROVAL_STAGE", "校内审核环节已完成")
                target = PlanStatus.SCHOOL_APPROVED if action == "approve" else PlanStatus.REJECTED
            elif actor.role == "control":
                if plan.status != PlanStatus.SCHOOL_APPROVED:
                    raise ApiError(409, "APPROVAL_STAGE", "需先完成校内审核")
                target = PlanStatus.CONTROL_APPROVED if action == "approve" else PlanStatus.REJECTED
            else:
                self._audit(actor, "approve_plan", plan_id, "DENIED", reason="role_forbidden")
                raise ApiError(403, "FORBIDDEN", "无审核权限")
            self._emit("PLAN_APPROVAL",
                       {"plan_id": plan_id, "status": target.value, "approval": approval})
            self._audit(actor, f"plan_{action}", plan_id, "ALLOWED", stage=approval["stage"])
            return plan

    def withdraw_plan(self, actor: Actor, plan_id: str, reason: str,
                      expected_version: Optional[int] = None) -> Plan:
        with self._lock:
            plan = self._get_plan_checked(actor, plan_id)
            if actor.role == "control":
                pass  # 总控可代执行
            elif actor.role != "school":
                self._audit(actor, "withdraw_plan", plan_id, "DENIED", reason="role_forbidden")
                raise ApiError(403, "FORBIDDEN", "无退出权限")
            if plan.status == PlanStatus.WITHDRAWN:
                return plan  # 幂等
            locked = self._plan_in_session(plan_id) is not None
            if locked:
                self._check_version(actor, expected_version, plan.school_id,
                                    {"plan_ids": [plan_id], "window_id": plan.window_id})
            hold = self.holds.get(f"hold-{plan_id}")
            if locked:
                session = self._plan_in_session(plan_id)
                others = [pid for a in session.allocations for pid in a.plan_ids
                          if pid != plan_id]
                self._rebuild_after_change(
                    actor, plan.window_id,
                    release_holds=[plan_id], plan_status=PlanStatus.WITHDRAWN,
                    reason=f"学校 {plan.school_id} 退出：{reason}",
                    notices={plan_id: "PLAN_WITHDRAWN"},
                    changed_kind_for_others="SESSION_CHANGED",
                    conflict_id=None, other_plan_ids=others)
            else:
                if hold and hold.status == HoldStatus.HELD:
                    self._emit("HOLD_RELEASED", {"id": hold.id})
                self._emit("PLAN_STATUS_CHANGED",
                           {"plan_id": plan_id, "status": PlanStatus.WITHDRAWN.value})
            self._audit(actor, "withdraw_plan", plan_id, "ALLOWED", reason=reason,
                        locked=locked)
            return self.plans[plan_id]

    def hold_plan(self, actor: Actor, plan_id: str) -> Hold:
        """总控对已通过两级审核的计划占位；并发下严格不超额。"""
        with self._lock:
            self._require_role(actor, {"control"}, "hold_plan", plan_id)
            plan = self.plans.get(plan_id)
            if plan is None:
                raise ApiError(404, "PLAN_NOT_FOUND", "计划不存在")
            hold_id = f"hold-{plan_id}"
            hold = self.holds.get(hold_id)
            if hold and hold.status == HoldStatus.HELD:
                return hold  # 幂等
            if plan.status not in (PlanStatus.CONTROL_APPROVED, PlanStatus.LOCKED):
                raise ApiError(409, "PLAN_NOT_APPROVED",
                               "计划需经总控审核通过后方可占位")
            window = self.windows[plan.window_id]
            capacity = self._window_capacity(window)
            used = self._used_seats(window.id)
            if used + plan.seats > capacity:
                self._audit(actor, "hold_plan", plan_id, "DENIED",
                            used=used, requested=plan.seats, capacity=capacity)
                raise ApiError(409, "CAPACITY_EXCEEDED",
                               f"窗口容量 {capacity} 席，已占 {used} 席，"
                               f"再占 {plan.seats} 席将超额",
                               {"used": used, "capacity": capacity,
                                "requested": plan.seats})
            self._emit("HOLD_HELD", {
                "id": hold_id, "plan_id": plan_id, "window_id": plan.window_id,
                "school_id": plan.school_id, "seats": plan.seats})
            self._audit(actor, "hold_plan", plan_id, "ALLOWED", seats=plan.seats)
            return self.holds[hold_id]

    # ---------------------------------------------------------- 排期锁定
    def lock_window(self, actor: Actor, window_id: str, reason: str) -> Session:
        with self._lock:
            self._require_role(actor, {"control"}, "lock_window", window_id)
            window = self.windows.get(window_id)
            if window is None:
                raise ApiError(404, "WINDOW_NOT_FOUND", "直播窗口不存在")
            active = [self.holds[hid] for hid in self._active_hold_ids(window_id)]
            if not active:
                raise ApiError(400, "NO_ACTIVE_HOLDS", "窗口内没有可锁定的占位")
            total = sum(h.seats for h in active)
            capacity = self._window_capacity(window)
            if total > capacity:
                # 窗口曾在锁定前缩容，旧占位超出新容量：禁止超额锁定，
                # 总控需先释放部分占位或重新调整窗口。
                self._audit(actor, "lock_window", window_id, "DENIED",
                            used=total, capacity=capacity, reason="capacity_exceeded")
                raise ApiError(409, "CAPACITY_EXCEEDED",
                               f"窗口容量 {capacity} 席，当前占位 {total} 席，无法锁定",
                               {"used": total, "capacity": capacity})
            session = self._build_session(window)
            old = self.current_sessions.get(window_id)
            old_ids = {pid for a in old.allocations for pid in a.plan_ids} if old else set()
            new_ids = {pid for a in session.allocations for pid in a.plan_ids}
            if old and _session_to_dict(old) == _session_to_dict(session):
                return old  # 幂等：阵容与窗口均无变化，不产生新版本
            for hold in active:
                plan = self.plans[hold.plan_id]
                if plan.status != PlanStatus.LOCKED:
                    self._emit("PLAN_STATUS_CHANGED",
                               {"plan_id": plan.id, "status": PlanStatus.LOCKED.value})
            self.current_sessions[window_id] = session
            self._emit("SCHEDULE_VERSION_BUMPED", self._snapshot_payload(
                f"锁定窗口 {window_id}：{reason}"))
            for pid in sorted(new_ids - old_ids):
                self._draft_notice(pid, "SESSION_CONFIRMED", None)
            self._audit(actor, "lock_window", window_id, "ALLOWED",
                        version=self.current_version, seats=sum(a.seats for a in session.allocations))
            return session

    def _rebuild_after_change(self, actor: Actor, window_id: str, *,
                              release_holds: list[str],
                              plan_status: Optional[PlanStatus],
                              reason: str, notices: dict[str, str],
                              changed_kind_for_others: str,
                              conflict_id: Optional[str],
                              other_plan_ids: Optional[list[str]] = None) -> int:
        """释放占位、按最新窗口重算场次并生成新版本。返回新版本号。"""
        for pid in release_holds:
            hold = self.holds.get(f"hold-{pid}")
            if hold and hold.status == HoldStatus.HELD:
                self._emit("HOLD_RELEASED", {"id": hold.id})
            if plan_status is not None:
                self._emit("PLAN_STATUS_CHANGED",
                           {"plan_id": pid, "status": plan_status.value})
        window = self.windows[window_id]
        remaining = self._active_hold_ids(window_id)
        if remaining:
            self.current_sessions[window_id] = self._build_session(window)
            for pid in self.plans:
                hold = self.holds.get(f"hold-{pid}")
                if hold and hold.window_id == window_id and hold.status == HoldStatus.HELD:
                    plan = self.plans[pid]
                    if plan.status != PlanStatus.LOCKED:
                        self._emit("PLAN_STATUS_CHANGED",
                                   {"plan_id": pid, "status": PlanStatus.LOCKED.value})
        else:
            self.current_sessions.pop(window_id, None)
        self._emit("SCHEDULE_VERSION_BUMPED", self._snapshot_payload(reason))
        version = self.current_version
        for pid, kind in notices.items():
            self._draft_notice(pid, kind, conflict_id)
        for pid in other_plan_ids or []:
            self._draft_notice(pid, changed_kind_for_others, conflict_id)
        return version

    # ---------------------------------------------------------- 窗口变更
    def revise_window(self, actor: Actor, window_id: str, start: str, end: str,
                      signal_capacity: int, reason: str,
                      expected_version: Optional[int]) -> dict:
        """总控调整窗口（窗口缩短等）。容量装不下时挂起冲突，等待处置。"""
        with self._lock:
            self._require_role(actor, {"control"}, "revise_window", window_id)
            window = self.windows.get(window_id)
            if window is None:
                raise ApiError(404, "WINDOW_NOT_FOUND", "直播窗口不存在")
            self._check_version(actor, expected_version, "control",
                                {"window_id": window_id})
            if signal_capacity <= 0:
                raise ApiError(400, "BAD_CAPACITY", "信号容量必须为正整数")
            old_capacity = self._window_capacity(window)
            new_window = Window(window_id, window.campus_id, start, end, signal_capacity)
            new_capacity = self._window_capacity(new_window)
            locked = self.current_sessions.get(window_id)
            active_ids = sorted(self._active_hold_ids(window_id),
                                key=lambda hid: self.holds[hid].plan_id)
            total = sum(self.holds[hid].seats for hid in active_ids)
            overflow = total - new_capacity
            self._emit("WINDOW_REVISED", new_window.__dict__.copy())
            self._audit(actor, "revise_window", window_id, "ALLOWED",
                        signal_capacity=signal_capacity, reason=reason)
            if locked and overflow > 0:
                # 确定性裁剪规则：按计划编号倒序调整（编号靠后上报最晚）。
                drop: list[str] = []
                freed = 0
                for hid in reversed(active_ids):
                    if freed >= overflow:
                        break
                    drop.append(self.holds[hid].plan_id)
                    freed += self.holds[hid].seats
                keep = [self.holds[hid].plan_id for hid in active_ids
                        if self.holds[hid].plan_id not in drop]
                conflict_id = f"conflict-{self._conflict_seq + 1}"
                reasons = [
                    f"窗口 {window_id} 可用席位由 {old_capacity} 调整为 {new_capacity}",
                    f"当前已锁定 {total} 席，超出 {overflow} 席",
                    f"按“上报编号靠后优先调整”规则，受影响计划：{', '.join(drop)}",
                ]
                affected = list(self.holds[hid].plan_id for hid in active_ids)
                self._emit("CONFLICT_DETECTED", {
                    "id": conflict_id,
                    "kind": ConflictKind.WINDOW_SHORTENED.value,
                    "detected_reasons": reasons,
                    "base_version": self.current_version,
                    "current_version": self.current_version,
                    "actor_side": "control",
                    "detail": {
                        "window_id": window_id,
                        "capacity_before": old_capacity,
                        "capacity_after": new_capacity,
                        "locked_seats": total, "overflow_seats": overflow,
                        "would_drop_plan_ids": drop,
                        "kept_plan_ids": keep,
                        "notification_scope": {
                            "window_id": window_id, "plan_ids": affected,
                            "schools": sorted({self.plans[p].school_id for p in affected}),
                        },
                    },
                    "options": [
                        {"choice": "DROP_LATEST", "label": "按上报顺序裁剪",
                         "description": "释放编号靠后计划的占位并通知其改期，其余保持锁定"},
                        {"choice": "CANCEL_SESSION", "label": "取消该场次",
                         "description": "释放全部占位，窗口内所有学校收到取消通知"},
                    ],
                    "suggested": "DROP_LATEST",
                })
                self._draft_control_notice("WINDOW_SHORTENED", affected, conflict_id)
                self._audit(actor, "window_conflict", f"conflict:{conflict_id}",
                            "CONFLICT", overflow=overflow, scope=affected)
                raise ApiError(409, "CONFLICT_OPEN",
                               "窗口缩短后容量不足，需选择处置方案",
                               {"conflict_id": conflict_id, "would_drop": drop})
            # 容量足够：直接重算出版本，并通知在场参与者。
            if locked:
                current_ids = [self.holds[hid].plan_id for hid in active_ids]
                self.current_sessions[window_id] = self._build_session(new_window)
                self._emit("SCHEDULE_VERSION_BUMPED", self._snapshot_payload(
                    f"窗口 {window_id} 时间/容量调整：{reason}"))
                for pid in current_ids:
                    self._draft_notice(pid, "SESSION_CHANGED", None)
            return {"version": self.current_version, "window": new_window.__dict__.copy()}

    def resolve_conflict(self, actor: Actor, conflict_id: str, choice: str) -> dict:
        with self._lock:
            self._require_role(actor, {"control"}, "resolve_conflict", conflict_id)
            conflict = self.conflicts.get(conflict_id)
            if conflict is None:
                raise ApiError(404, "CONFLICT_NOT_FOUND", "冲突不存在")
            if conflict.status == ConflictStatus.RESOLVED:
                raise ApiError(409, "CONFLICT_CLOSED", "冲突已处置")
            valid = {o["choice"] for o in conflict.options}
            if choice not in valid:
                raise ApiError(400, "BAD_CHOICE",
                               f"无效方案，可选：{sorted(valid)}")
            self._emit("CONFLICT_RESOLVED", {"id": conflict_id, "choice": choice})
            result: dict = {"conflict_id": conflict_id, "choice": choice}
            if conflict.kind == ConflictKind.SIMULTANEOUS_EDIT:
                self._audit(actor, "resolve_conflict", conflict_id, "ALLOWED",
                            choice=choice)
                result["version"] = self.current_version
                return result
            window_id = conflict.detail["window_id"]
            affected = conflict.detail["notification_scope"]["plan_ids"]
            if choice == "DROP_LATEST":
                drop = conflict.detail["would_drop_plan_ids"]
                keep = [pid for pid in affected if pid not in drop]
                version = self._rebuild_after_change(
                    actor, window_id, release_holds=drop,
                    plan_status=PlanStatus.CONTROL_APPROVED,
                    reason=f"窗口 {window_id} 缩短，按方案裁剪 {len(drop)} 个计划",
                    notices={pid: "PLAN_REMOVED" for pid in drop},
                    changed_kind_for_others="SESSION_CHANGED",
                    conflict_id=conflict_id, other_plan_ids=keep)
            else:  # CANCEL_SESSION
                version = self._rebuild_after_change(
                    actor, window_id, release_holds=affected,
                    plan_status=PlanStatus.CONTROL_APPROVED,
                    reason=f"窗口 {window_id} 场次取消",
                    notices={pid: "SESSION_CANCELLED" for pid in affected},
                    changed_kind_for_others="SESSION_CHANGED",
                    conflict_id=conflict_id)
            self._audit(actor, "resolve_conflict", conflict_id, "ALLOWED",
                        choice=choice, version=version)
            result["version"] = version
            return result

    # ---------------------------------------------------------- 通知派发
    def dispatch_notices(self, actor: Actor) -> dict:
        """派发前按当前最新排期逐条复核，失效通知一律抑制且绝不误发。"""
        with self._lock:
            self._require_role(actor, {"control"}, "dispatch_notices", "notices")
            dispatched, suppressed, waiting = [], [], []
            pii_touched = False
            # 每个计划只以其最新一条待发通知为准：更早的“场次确认”被取代。
            latest_pending: dict[str, str] = {}
            for notice in self.notices.values():
                if (notice.status == NoticeStatus.PENDING
                        and notice.audience == "participants"
                        and notice.scope_plan_ids):
                    latest_pending[notice.scope_plan_ids[0]] = notice.id
            open_windows = {
                c.detail["window_id"] for c in self.conflicts.values()
                if c.status == ConflictStatus.OPEN
                and c.kind == ConflictKind.WINDOW_SHORTENED}
            for notice in list(self.notices.values()):
                if notice.status != NoticeStatus.PENDING:
                    continue
                if notice.conflict_id:
                    conflict = self.conflicts.get(notice.conflict_id)
                    if conflict and conflict.status == ConflictStatus.OPEN:
                        waiting.append(notice.id)
                        continue
                plan_id = notice.scope_plan_ids[0] if notice.audience == "participants" else None
                suppress_reason = None
                if plan_id is not None:
                    plan = self.plans.get(plan_id)
                    if latest_pending.get(plan_id) != notice.id:
                        # 同一计划已有更新的待发通知，旧草稿被取代。
                        suppress_reason = "SUPERSEDED_BY_NEWER_NOTICE"
                    elif plan and plan.window_id in open_windows and notice.kind in (
                            "SESSION_CONFIRMED", "SESSION_CHANGED"):
                        # 窗口冲突未处置前挂起，避免处置结果出来后通知已误发。
                        waiting.append(notice.id)
                        continue
                    else:
                        in_session = self._plan_in_session(plan_id) is not None
                        if notice.kind in ("SESSION_CONFIRMED", "SESSION_CHANGED"):
                            if not in_session:
                                suppress_reason = "PLAN_NO_LONGER_IN_SESSION"
                        elif notice.kind in ("PLAN_REMOVED", "SESSION_CANCELLED"):
                            if in_session:
                                suppress_reason = "PLAN_REJOINED_SESSION"
                            elif plan and plan.status == PlanStatus.WITHDRAWN:
                                suppress_reason = "SUPERSEDED_BY_WITHDRAWAL"
                        elif notice.kind == "PLAN_WITHDRAWN":
                            if not plan or plan.status != PlanStatus.WITHDRAWN:
                                suppress_reason = "PLAN_NOT_WITHDRAWN"
                if suppress_reason:
                    self._emit("NOTICE_SUPPRESSED",
                               {"id": notice.id, "reason": suppress_reason})
                    suppressed.append({"id": notice.id, "reason": suppress_reason})
                    continue
                if notice.audience == "control":
                    recipients = ["control-desk"]
                else:
                    pii_touched = True
                    recipients = [self.students[sid].contact
                                  for sid in self.plans[plan_id].student_ids
                                  if sid in self.students]
                self._emit("NOTICE_DISPATCHED",
                           {"id": notice.id, "recipients": recipients})
                dispatched.append({"id": notice.id, "recipients": recipients})
            if pii_touched:
                self._audit(actor, "dispatch_resolve_contacts", "students",
                            "ALLOWED", pii_access=True)
            self._audit(actor, "dispatch_notices", "notices", "ALLOWED",
                        dispatched=len(dispatched), suppressed=len(suppressed),
                        waiting=len(waiting))
            return {"dispatched": dispatched, "suppressed": suppressed, "waiting": waiting}

    # ---------------------------------------------------------- 查询
    def plan_view(self, actor: Actor, plan_id: str) -> dict:
        with self._lock:
            plan = self._get_plan_checked(actor, plan_id)
            return self._plan_dict(plan)

    def _plan_dict(self, plan: Plan) -> dict:
        return {
            "id": plan.id, "school_id": plan.school_id, "window_id": plan.window_id,
            "seats": plan.seats, "student_ids": list(plan.student_ids),
            "questions": [_question_to_dict(q) for q in plan.questions],
            "materials": list(plan.materials), "status": plan.status.value,
            "approvals": list(plan.approvals), "version": plan.version,
        }

    def schedule_view(self) -> dict:
        return {
            "version": self.current_version,
            "sessions": [_session_to_dict(s) for s in
                         sorted(self.current_sessions.values(), key=lambda s: s.window_id)],
        }

    def version_view(self, version: int) -> dict:
        if version <= 0 or version > self.current_version:
            raise ApiError(404, "VERSION_NOT_FOUND", f"排期版本 v{version} 不存在")
        sv = self.versions[version - 1]
        return {"version": sv.version, "reason": sv.reason, "at": sv.at,
                "sessions": [_session_to_dict(s) for s in sv.sessions]}

    def conflict_view(self, conflict: ConflictRecord) -> dict:
        return {
            "id": conflict.id, "kind": conflict.kind.value,
            "status": conflict.status.value,
            "detected_reasons": conflict.detected_reasons,
            "base_version": conflict.base_version,
            "current_version": conflict.current_version,
            "detail": conflict.detail, "options": conflict.options,
            "suggested": conflict.suggested, "resolution": conflict.resolution,
        }

    def notice_view(self, notice: Notice) -> dict:
        return {
            "id": notice.id, "kind": notice.kind, "status": notice.status.value,
            "basis_version": notice.basis_version, "conflict_id": notice.conflict_id,
            "scope_plan_ids": notice.scope_plan_ids, "audience": notice.audience,
            "suppressed_reason": notice.suppressed_reason,
            "recipients": notice.recipients,
        }

    # ---------------------------------------------------------- 断线重连
    def sync(self, actor: Actor, after_seq: int) -> dict:
        with self._lock:
            events = []
            for event in self.store.since_seq(after_seq):
                filtered = self._filter_event_for(actor, event)
                if filtered is not None:
                    events.append(filtered)
            return {
                "cursor": self.store.all_events[-1]["seq"] if self.store.all_events else 0,
                "schedule_version": self.current_version,
                "events": events,
            }

    def _filter_event_for(self, actor: Actor, event: dict) -> Optional[dict]:
        etype = event["type"]
        p = event["payload"]
        slim = {"seq": event["seq"], "type": etype, "event_time": event["event_time"]}

        if etype == "AUDIT_ENTRY":
            return None if actor.role == "school" else {**slim, "payload": p}
        if etype == "STUDENT_ADDED":
            if actor.role == "school" and actor.school_id == p["school_id"]:
                return {**slim, "payload": p}
            if actor.role == "control":
                return {**slim, "payload": {k: v for k, v in p.items()
                                            if k not in ("name", "contact")}}
            return None
        if etype in ("PLAN_SUBMITTED", "PLAN_APPROVAL", "PLAN_STATUS_CHANGED"):
            plan = self.plans.get(p.get("plan_id"))
            school_id = p.get("school_id") or (plan.school_id if plan else None)
            if actor.role == "school" and actor.school_id != school_id:
                return None
            return {**slim, "payload": p}
        if etype in ("HOLD_HELD", "HOLD_RELEASED"):
            hold = self.holds.get(p["id"])
            school_id = p.get("school_id") or (hold.school_id if hold else None)
            if actor.role == "school" and actor.school_id != school_id:
                return None
            payload = dict(p)
            if school_id:
                payload["school_id"] = school_id
            return {**slim, "payload": payload}
        if etype in ("NOTICE_DRAFTED", "NOTICE_SUPPRESSED", "NOTICE_DISPATCHED"):
            if p["audience"] == "control" and actor.role != "control":
                return None
            if actor.role == "school":
                plan_id = p["scope_plan_ids"][0]
                plan = self.plans.get(plan_id)
                if not plan or plan.school_id != actor.school_id:
                    return None
            return {**slim, "payload": p}
        # 窗口、场次版本、冲突：所有已认证角色可见（仅含标识，不含个人资料）
        return {**slim, "payload": p}
