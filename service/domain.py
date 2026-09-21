"""天地课堂联播台领域核心。

设计要点：
- 事件溯源：所有状态变更先落事件日志（append-only），再更新内存投影；
  重启后按 seq 重放即可恢复，每次排期版本可用 schedule_at() 还原。
- 串行命令：一把锁保护所有命令，容量校验与占位在同一临界区完成，
  保证并发占位不会超额。
- 幂等：命令可携带 request_id（HTTP Idempotency-Key），结果随事件持久化，
  断线后重放同一键返回原结果且不重复落事件；业务 ID 本身亦天然幂等。
- 可解释冲突：排期变更携带版本号与中文摘要；expected_version 不符时返回
  期间全部变更说明；窗口收缩给出影响面与处置选项，通知范围显式列出。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .store import EventStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DomainError(Exception):
    """业务规则冲突，携带 HTTP 状态码与结构化说明。"""

    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra or {}

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def _parse_time(value, field):
    if not isinstance(value, str):
        raise DomainError("invalid_time", f"{field} 必须是 ISO 8601 字符串")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise DomainError("invalid_time", f"{field} 不是合法的 ISO 8601 时间: {value!r}")


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_field", f"缺少或非法字段: {field}")
    return value.strip()


def _require_int(payload: dict, field: str, minimum: int = 1) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DomainError("invalid_field", f"字段 {field} 必须是不小于 {minimum} 的整数")
    return value


REVIEW_STAGES = ("initial", "final")
PLAN_ACTIVE_STATUSES = ("submitted", "initial_approved", "approved")


class Domain:
    """领域状态与命令。所有公开方法线程安全。"""

    def __init__(self, store: EventStore):
        self.store = store
        self._lock = threading.RLock()
        self._reset()
        for event in self.store.events:
            self._project(event)

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------
    def _reset(self) -> None:
        self.schools: dict[str, dict] = {}
        self.windows: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.students: dict[str, list] = {}          # plan_id -> 学生名单（受角色隔离）
        self.notifications: dict[str, dict] = {}
        self.audit_trail: list[dict] = []            # 未成年人资料访问审计
        self.schedule_version = 0
        self.schedule_log: list[dict] = []           # 每次排期变更的可解释摘要
        self.idempotency: dict[str, dict] = {}       # request_id -> 已持久化的结果

    def _project(self, event: dict) -> None:
        """把一条事件应用到投影；启动重放与在线命令共用此路径。"""
        etype = event["type"]
        p = event["payload"]
        if etype == "school_registered":
            self.schools[p["school_id"]] = {
                "school_id": p["school_id"], "name": p["name"], "region": p["region"],
                "seat_capacity": p["seat_capacity"], "registered_at": event["recorded_at"],
            }
        elif etype == "window_registered":
            self.windows[p["window_id"]] = {
                "window_id": p["window_id"], "start": p["start"], "end": p["end"],
                "seat_capacity": p["seat_capacity"], "registered_at": event["recorded_at"],
            }
        elif etype == "plan_submitted":
            self.plans[p["plan_id"]] = {
                "plan_id": p["plan_id"], "school_id": p["school_id"], "window_id": p["window_id"],
                "student_count": p["student_count"], "status": "submitted", "review_chain": [],
                "session_id": None, "submitted_at": event["recorded_at"],
                "approved_at": None, "withdrawn_reason": None,
            }
        elif etype == "plan_reviewed":
            plan = self.plans[p["plan_id"]]
            plan["review_chain"].append({
                "stage": p["stage"], "action": p["action"], "actor": event["actor"]["id"],
                "comment": p.get("comment"), "at": event["recorded_at"],
            })
            plan["status"] = p["status"]
            if p["status"] == "approved":
                plan["approved_at"] = event["recorded_at"]
        elif etype == "plan_withdrawn":
            plan = self.plans[p["plan_id"]]
            plan["status"] = "withdrawn"
            plan["withdrawn_reason"] = p.get("reason")
        elif etype == "students_recorded":
            self.students[p["plan_id"]] = [dict(s) for s in p["students"]]
        elif etype == "session_locked":
            assignments = [dict(a) for a in p["assignments"]]
            self.sessions[p["session_id"]] = {
                "session_id": p["session_id"], "window_id": p["window_id"], "status": "locked",
                "assignments": assignments, "locked_at": event["recorded_at"], "cancelled_reason": None,
            }
            for a in assignments:
                self.plans[a["plan_id"]]["session_id"] = p["session_id"]
        elif etype == "assignment_removed":
            sess = self.sessions[p["session_id"]]
            sess["assignments"] = [a for a in sess["assignments"] if a["plan_id"] != p["plan_id"]]
            self.plans[p["plan_id"]]["session_id"] = None
        elif etype == "session_cancelled":
            sess = self.sessions[p["session_id"]]
            sess["status"] = "cancelled"
            sess["cancelled_reason"] = p.get("reason")
            for a in sess["assignments"]:
                self.plans[a["plan_id"]]["session_id"] = None
            sess["assignments"] = []
        elif etype == "window_changed":
            window = self.windows[p["window_id"]]
            window["end"] = p["new_end"]
            window["seat_capacity"] = p["new_seat_capacity"]
        elif etype == "notification_queued":
            self.notifications[p["notification_id"]] = {
                "notification_id": p["notification_id"], "school_id": p.get("school_id"),
                "kind": p["kind"], "message": p["message"], "refs": p.get("refs", {}),
                "status": "pending", "queued_at": event["recorded_at"],
                "superseded_reason": None, "dispatched_at": None,
            }
        elif etype == "notification_superseded":
            n = self.notifications[p["notification_id"]]
            n["status"] = "superseded"
            n["superseded_reason"] = p.get("reason")
        elif etype == "notification_dispatched":
            n = self.notifications[p["notification_id"]]
            n["status"] = "sent"
            n["dispatched_at"] = event["recorded_at"]
        elif etype == "minor_data_accessed":
            self.audit_trail.append({
                "seq": event["seq"], "at": event["recorded_at"], "actor_id": p["actor_id"],
                "actor_role": p["actor_role"], "plan_id": p["plan_id"], "purpose": p["purpose"],
            })
        # 排期版本与可解释日志
        if event.get("version"):
            self.schedule_version = max(self.schedule_version, event["version"])
            self.schedule_log.append({
                "version": event["version"], "seq": event["seq"], "type": etype,
                "summary": event.get("summary"), "actor": event["actor"]["id"],
                "at": event["recorded_at"], "affected_schools": event.get("affected_schools", []),
            })
        # 幂等表（结果随事件持久化，重启后重放同一键仍返回原结果）
        if event.get("request_id") and event.get("result") is not None:
            self.idempotency[event["request_id"]] = event["result"]

    # ------------------------------------------------------------------
    # 事件写入
    # ------------------------------------------------------------------
    def _emit(self, actor, event_type: str, payload: dict, request_id: str | None = None,
              occurred_at: str | None = None, schedule: bool = False, summary: str | None = None,
              affected_schools=(), result: dict | None = None) -> dict:
        """构造并落一条事件（调用方须已持锁并完成校验）。"""
        event = {
            "type": event_type,
            "recorded_at": _now(),
            "actor": {"id": actor.id, "role": actor.role, "school_id": actor.school_id},
            "request_id": request_id,
            "occurred_at": occurred_at,
            "payload": payload,
        }
        if schedule:
            self.schedule_version += 1
            event["version"] = self.schedule_version
            event["summary"] = summary
            event["affected_schools"] = sorted(affected_schools)
        if result is not None:
            event["result"] = result
        self.store.append(event)
        self._project(event)
        return event

    def _replay(self, request_id: str | None):
        """命中幂等表则返回已持久化的结果，否则 None。"""
        if request_id and request_id in self.idempotency:
            return self.idempotency[request_id]
        return None

    def _next_version(self) -> int:
        return self.schedule_version + 1

    def _require_version(self, expected) -> None:
        """乐观并发：expected_version 不符时给出可解释的冲突说明。"""
        if expected is None:
            raise DomainError(
                "version_required",
                "该操作需要 expected_version（当前排期版本可通过 GET /schedule 获取）",
            )
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise DomainError("invalid_field", "expected_version 必须是整数")
        if expected != self.schedule_version:
            intervening = [e for e in self.schedule_log if e["version"] > expected]
            raise DomainError(
                "version_conflict",
                f"排期版本已变化：期望 v{expected}，当前 v{self.schedule_version}，"
                f"期间有 {len(intervening)} 次变更",
                409,
                {
                    "expected_version": expected,
                    "current_version": self.schedule_version,
                    "intervening": intervening,
                    "recovery": f"请先 GET /schedule/versions/{self.schedule_version} 查看最新排期，再基于新版本重试",
                },
            )

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------
    def _window_usage(self, window_id: str) -> int:
        return sum(
            a["seats"]
            for s in self.sessions.values()
            if s["window_id"] == window_id and s["status"] == "locked"
            for a in s["assignments"]
        )

    def _trim_candidates(self, window_id: str) -> list[dict]:
        """窗口收缩时的移除候选：按终审通过时间从新到旧（同刻按 plan_id 倒序）。"""
        candidates = []
        for sess in self.sessions.values():
            if sess["window_id"] != window_id or sess["status"] != "locked":
                continue
            for a in sess["assignments"]:
                plan = self.plans[a["plan_id"]]
                candidates.append({
                    "plan_id": a["plan_id"], "school_id": a["school_id"], "seats": a["seats"],
                    "session_id": sess["session_id"], "approved_at": plan["approved_at"] or "",
                })
        candidates.sort(key=lambda c: (c["approved_at"], c["plan_id"]), reverse=True)
        return candidates

    def _notify(self, actor, school_id, kind: str, message: str, refs: dict) -> str:
        nid = f"ntf-{len(self.notifications) + 1:05d}"
        self._emit(actor, "notification_queued", {
            "notification_id": nid, "school_id": school_id,
            "kind": kind, "message": message, "refs": refs,
        })
        return nid

    def _pending_notifications(self, predicate) -> list[dict]:
        return [n for n in self.notifications.values() if n["status"] == "pending" and predicate(n)]

    def _supersede(self, actor, targets: list[dict], reason: str) -> None:
        for n in targets:
            self._emit(actor, "notification_superseded",
                       {"notification_id": n["notification_id"], "reason": reason})

    # ------------------------------------------------------------------
    # 学校与窗口
    # ------------------------------------------------------------------
    def register_school(self, actor, payload: dict, request_id: str | None = None,
                        occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            school_id = _require_str(payload, "school_id")
            name = _require_str(payload, "name")
            region = _require_str(payload, "region")
            capacity = _require_int(payload, "seat_capacity")
            existing = self.schools.get(school_id)
            if existing:
                if (existing["name"], existing["region"], existing["seat_capacity"]) == (name, region, capacity):
                    return dict(existing), True
                raise DomainError("already_exists", f"学校 {school_id} 已登记且资料不一致", 409)
            result = {"school_id": school_id, "name": name, "region": region,
                      "seat_capacity": capacity, "status": "registered"}
            self._emit(actor, "school_registered",
                       {"school_id": school_id, "name": name, "region": region, "seat_capacity": capacity},
                       request_id, occurred_at, result=result)
            return result, False

    def register_window(self, actor, payload: dict, request_id: str | None = None,
                        occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            window_id = _require_str(payload, "window_id")
            start_s, end_s = _require_str(payload, "start"), _require_str(payload, "end")
            start, end = _parse_time(start_s, "start"), _parse_time(end_s, "end")
            if end <= start:
                raise DomainError("invalid_time", "窗口结束时间必须晚于开始时间")
            capacity = _require_int(payload, "seat_capacity")
            existing = self.windows.get(window_id)
            if existing:
                if (existing["start"], existing["end"], existing["seat_capacity"]) == (start_s, end_s, capacity):
                    return dict(existing), True
                raise DomainError("already_exists", f"窗口 {window_id} 已登记且参数不一致", 409)
            for w in self.windows.values():
                ws, we = _parse_time(w["start"], "start"), _parse_time(w["end"], "end")
                if start < we and end > ws:
                    raise DomainError("window_overlap",
                                      f"窗口与已登记窗口 {w['window_id']} 时间重叠", 409,
                                      {"conflict_with": w["window_id"]})
            result = {"window_id": window_id, "start": start_s, "end": end_s,
                      "seat_capacity": capacity, "status": "registered"}
            self._emit(actor, "window_registered",
                       {"window_id": window_id, "start": start_s, "end": end_s, "seat_capacity": capacity},
                       request_id, occurred_at, result=result)
            return result, False

    # ------------------------------------------------------------------
    # 参与计划与审核链
    # ------------------------------------------------------------------
    def submit_plan(self, actor, payload: dict, request_id: str | None = None,
                    occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            plan_id = _require_str(payload, "plan_id")
            school_id = _require_str(payload, "school_id")
            window_id = _require_str(payload, "window_id")
            count = _require_int(payload, "student_count")
            if actor.role == "school" and actor.school_id != school_id:
                raise DomainError("forbidden", "学校只能为本校提交参与计划", 403)
            school = self.schools.get(school_id)
            if not school:
                raise DomainError("school_not_found", f"学校 {school_id} 未登记", 404)
            if window_id not in self.windows:
                raise DomainError("window_not_found", f"窗口 {window_id} 未登记", 404)
            if count > school["seat_capacity"]:
                raise DomainError("capacity_exceeded",
                                  f"计划人数 {count} 超出校区容量 {school['seat_capacity']}", 409,
                                  {"school_seat_capacity": school["seat_capacity"]})
            existing = self.plans.get(plan_id)
            if existing:
                same = (existing["school_id"], existing["window_id"], existing["student_count"]) == (school_id, window_id, count)
                if same:
                    return self._plan_view(existing), True
                raise DomainError("already_exists", f"计划 {plan_id} 已提交且内容不一致", 409)
            for p in self.plans.values():
                if (p["school_id"], p["window_id"]) == (school_id, window_id) and p["status"] in PLAN_ACTIVE_STATUSES:
                    raise DomainError("duplicate_plan",
                                      f"学校 {school_id} 在窗口 {window_id} 已有进行中的计划 {p['plan_id']}", 409,
                                      {"existing_plan_id": p["plan_id"]})
            result = {"plan_id": plan_id, "school_id": school_id, "window_id": window_id,
                      "student_count": count, "status": "submitted"}
            self._emit(actor, "plan_submitted",
                       {"plan_id": plan_id, "school_id": school_id, "window_id": window_id,
                        "student_count": count},
                       request_id, occurred_at, result=result)
            return result, False

    def review_plan(self, actor, plan_id: str, payload: dict, request_id: str | None = None,
                    occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            plan = self.plans.get(plan_id)
            if not plan:
                raise DomainError("plan_not_found", f"计划 {plan_id} 不存在", 404)
            stage = _require_str(payload, "stage")
            action = _require_str(payload, "action")
            comment = payload.get("comment")
            if stage not in REVIEW_STAGES:
                raise DomainError("invalid_field", f"stage 必须是 {'/'.join(REVIEW_STAGES)}")
            if action not in ("approve", "reject"):
                raise DomainError("invalid_field", "action 必须是 approve/reject")
            if stage == "initial" and actor.role not in ("reviewer", "control"):
                raise DomainError("forbidden", "初审需要 reviewer 或 control 角色", 403)
            if stage == "final" and actor.role != "control":
                raise DomainError("forbidden", "终审需要 control 角色", 403)
            required_status = "submitted" if stage == "initial" else "initial_approved"
            if plan["status"] != required_status:
                raise DomainError("invalid_state",
                                  f"计划当前状态为 {plan['status']}，不能进行{'初审' if stage == 'initial' else '终审'}",
                                  409, {"current_status": plan["status"], "review_chain": plan["review_chain"]})
            new_status = {"initial": "initial_approved", "final": "approved"}[stage] if action == "approve" else "rejected"
            stage_cn = "初审" if stage == "initial" else "终审"
            result = {"plan_id": plan_id, "stage": stage, "status": new_status}
            self._emit(actor, "plan_reviewed",
                       {"plan_id": plan_id, "stage": stage, "action": action,
                        "comment": comment, "status": new_status},
                       request_id, occurred_at, result=result)
            self._notify(actor, plan["school_id"], "review_decision",
                         f"参与计划 {plan_id} {stage_cn}{'通过' if action == 'approve' else '未通过'}",
                         {"plan_id": plan_id})
            return result, False

    def withdraw_plan(self, actor, plan_id: str, payload: dict, request_id: str | None = None,
                      occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            plan = self.plans.get(plan_id)
            if not plan:
                raise DomainError("plan_not_found", f"计划 {plan_id} 不存在", 404)
            if actor.role == "school" and actor.school_id != plan["school_id"]:
                raise DomainError("forbidden", "学校只能撤回本校计划", 403)
            if plan["status"] not in PLAN_ACTIVE_STATUSES:
                raise DomainError("invalid_state",
                                  f"计划当前状态为 {plan['status']}，不能撤回", 409,
                                  {"current_status": plan["status"]})
            reason = payload.get("reason") or "学校退出"
            school_id = plan["school_id"]
            freed, session_id, superseded_ids = 0, None, []
            # 先确定影响面，再统一落事件，保证结果可解释
            sess = self.sessions.get(plan["session_id"] or "")
            if sess and sess["status"] == "locked":
                session_id = sess["session_id"]
                seats = next(a["seats"] for a in sess["assignments"] if a["plan_id"] == plan_id)
                freed = seats
                targets = self._pending_notifications(
                    lambda n: n["school_id"] == school_id
                    and n["refs"].get("session_id") == session_id
                    and n["kind"] == "session_locked")
                superseded_ids = [n["notification_id"] for n in targets]
                self._emit(actor, "assignment_removed",
                           {"session_id": session_id, "plan_id": plan_id, "school_id": school_id,
                            "seats": seats, "reason": "学校退出"},
                           schedule=True,
                           summary=f"学校 {school_id} 退出场次 {session_id}，释放 {seats} 个座位",
                           affected_schools=[school_id])
            result = {"plan_id": plan_id, "status": "withdrawn", "freed_seats": freed,
                      "superseded_notifications": superseded_ids,
                      "notification_scope": {"schools": [], "control": True},
                      "notifications_queued": 1}
            self._emit(actor, "plan_withdrawn", {"plan_id": plan_id, "reason": reason},
                       request_id, occurred_at, result=result)
            if superseded_ids:
                self._supersede(actor, [self.notifications[i] for i in superseded_ids],
                                "学校已退出，原场次占位通知失效")
            message = f"学校 {school_id} 退出计划 {plan_id}"
            if freed:
                message += f"，释放 {freed} 个座位（场次 {session_id}）"
            self._notify(actor, None, "plan_withdrawn", message,
                         {"plan_id": plan_id, "session_id": session_id})
            return result, False

    # ------------------------------------------------------------------
    # 未成年人资料（角色隔离 + 访问审计）
    # ------------------------------------------------------------------
    def record_students(self, actor, plan_id: str, payload: dict, request_id: str | None = None,
                        occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            plan = self.plans.get(plan_id)
            if not plan:
                raise DomainError("plan_not_found", f"计划 {plan_id} 不存在", 404)
            if actor.role == "school" and actor.school_id != plan["school_id"]:
                raise DomainError("forbidden", "学校只能登记本校计划的学生名单", 403)
            if plan["status"] in ("withdrawn", "rejected"):
                raise DomainError("invalid_state",
                                  f"计划已{('撤回' if plan['status'] == 'withdrawn' else '驳回')}，不能登记学生名单",
                                  409, {"current_status": plan["status"]})
            students = payload.get("students")
            if not isinstance(students, list) or not students:
                raise DomainError("invalid_field", "students 必须是非空名单")
            seen = set()
            for s in students:
                if not isinstance(s, dict):
                    raise DomainError("invalid_field", "学生记录必须是对象")
                sid = _require_str(s, "student_id")
                _require_str(s, "name")
                _require_str(s, "grade")
                if sid in seen:
                    raise DomainError("invalid_field", f"学生 {sid} 重复")
                seen.add(sid)
            if len(students) > plan["student_count"]:
                raise DomainError("capacity_exceeded",
                                  f"登记人数 {len(students)} 超出计划人数 {plan['student_count']}", 409)
            normalized = [{"student_id": s["student_id"].strip(), "name": s["name"].strip(),
                           "grade": s["grade"].strip()} for s in students]
            result = {"plan_id": plan_id, "recorded": len(normalized)}
            self._emit(actor, "students_recorded", {"plan_id": plan_id, "students": normalized},
                       request_id, occurred_at, result=result)
            return result, False

    def get_students(self, actor, plan_id: str, purpose: str | None):
        """读取学生名单：仅总控或本校，且必须说明用途；每次访问写入审计。"""
        with self._lock:
            plan = self.plans.get(plan_id)
            if not plan:
                raise DomainError("plan_not_found", f"计划 {plan_id} 不存在", 404)
            if not purpose or not purpose.strip():
                raise DomainError("purpose_required", "访问未成年人资料必须通过 purpose 说明用途")
            allowed = actor.role == "control" or (
                actor.role == "school" and actor.school_id == plan["school_id"])
            if not allowed:
                raise DomainError("minor_data_forbidden",
                                  "未成年人资料仅总控与所属学校可见", 403)
            self._emit(actor, "minor_data_accessed",
                       {"plan_id": plan_id, "actor_id": actor.id,
                        "actor_role": actor.role, "purpose": purpose.strip()})
            return {"plan_id": plan_id,
                    "students": [dict(s) for s in self.students.get(plan_id, [])]}

    # ------------------------------------------------------------------
    # 场次锁定 / 取消 / 窗口收缩
    # ------------------------------------------------------------------
    def lock_session(self, actor, payload: dict, request_id: str | None = None,
                     occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            expected = payload.get("expected_version")
            if expected is not None:
                self._require_version(expected)
            session_id = _require_str(payload, "session_id")
            window_id = _require_str(payload, "window_id")
            window = self.windows.get(window_id)
            if not window:
                raise DomainError("window_not_found", f"窗口 {window_id} 未登记", 404)
            assignments = payload.get("assignments")
            if not isinstance(assignments, list) or not assignments:
                raise DomainError("invalid_field", "assignments 必须是非空列表")
            normalized, seen = [], set()
            for a in assignments:
                if not isinstance(a, dict):
                    raise DomainError("invalid_field", "assignments 元素必须是对象")
                plan_id = _require_str(a, "plan_id")
                seats = _require_int(a, "seats")
                if plan_id in seen:
                    raise DomainError("invalid_field", f"计划 {plan_id} 在占位列表中重复")
                seen.add(plan_id)
                plan = self.plans.get(plan_id)
                if not plan:
                    raise DomainError("plan_not_found", f"计划 {plan_id} 不存在", 404)
                if plan["status"] != "approved":
                    raise DomainError("plan_not_approved",
                                      f"计划 {plan_id} 状态为 {plan['status']}，未通过终审", 409,
                                      {"plan_id": plan_id, "current_status": plan["status"]})
                if plan["window_id"] != window_id:
                    raise DomainError("window_mismatch",
                                      f"计划 {plan_id} 属于窗口 {plan['window_id']}，不能占位于 {window_id}", 409)
                if plan["session_id"]:
                    raise DomainError("plan_already_assigned",
                                      f"计划 {plan_id} 已占位于场次 {plan['session_id']}", 409,
                                      {"session_id": plan["session_id"]})
                if seats > plan["student_count"]:
                    raise DomainError("capacity_exceeded",
                                      f"占位 {seats} 超出计划 {plan_id} 的人数 {plan['student_count']}", 409)
                school = self.schools[plan["school_id"]]
                if seats > school["seat_capacity"]:
                    raise DomainError("capacity_exceeded",
                                      f"占位 {seats} 超出校区 {school['school_id']} 容量 {school['seat_capacity']}", 409)
                normalized.append({"plan_id": plan_id, "school_id": plan["school_id"], "seats": seats})
            normalized.sort(key=lambda a: a["plan_id"])
            existing = self.sessions.get(session_id)
            if existing:
                if existing["window_id"] == window_id and existing["assignments"] == normalized:
                    return self._session_view(existing), True
                raise DomainError("already_exists", f"场次 {session_id} 已锁定且内容不一致", 409)
            total = sum(a["seats"] for a in normalized)
            used = self._window_usage(window_id)
            remaining = window["seat_capacity"] - used
            if total > remaining:
                raise DomainError("capacity_exceeded",
                                  f"窗口 {window_id} 剩余 {remaining} 座，本次请求 {total} 座", 409,
                                  {"seat_capacity": window["seat_capacity"], "used_seats": used,
                                   "requested_seats": total, "remaining_seats": remaining})
            schools = sorted({a["school_id"] for a in normalized})
            version = self._next_version()
            result = {"session_id": session_id, "window_id": window_id, "version": version,
                      "seats_total": total, "assignments": normalized,
                      "notification_scope": {"schools": schools, "control": False},
                      "notifications_queued": len(schools)}
            self._emit(actor, "session_locked",
                       {"session_id": session_id, "window_id": window_id, "assignments": normalized},
                       request_id, occurred_at, schedule=True,
                       summary=f"场次 {session_id} 锁定：窗口 {window_id}，"
                               f"{len(normalized)} 所学校共 {total} 人",
                       affected_schools=schools, result=result)
            for a in normalized:
                self._notify(actor, a["school_id"], "session_locked",
                             f"已锁定场次 {session_id}：贵校 {a['seats']} 个座位（窗口 {window_id}）",
                             {"session_id": session_id, "plan_id": a["plan_id"], "window_id": window_id})
            return result, False

    def cancel_session(self, actor, session_id: str, payload: dict, request_id: str | None = None,
                       occurred_at: str | None = None):
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            sess = self.sessions.get(session_id)
            if not sess:
                raise DomainError("session_not_found", f"场次 {session_id} 不存在", 404)
            if sess["status"] != "locked":
                raise DomainError("invalid_state", f"场次当前状态为 {sess['status']}，不能取消", 409,
                                  {"current_status": sess["status"]})
            self._require_version(payload.get("expected_version"))
            reason = payload.get("reason") or "总控取消"
            schools = sorted({a["school_id"] for a in sess["assignments"]})
            freed = sum(a["seats"] for a in sess["assignments"])
            targets = self._pending_notifications(
                lambda n: n["refs"].get("session_id") == session_id)
            superseded_ids = [n["notification_id"] for n in targets]
            version = self._next_version()
            result = {"session_id": session_id, "status": "cancelled", "version": version,
                      "freed_seats": freed, "superseded_notifications": superseded_ids,
                      "notification_scope": {"schools": schools, "control": True},
                      "notifications_queued": len(schools) + 1}
            self._emit(actor, "session_cancelled", {"session_id": session_id, "reason": reason},
                       request_id, occurred_at, schedule=True,
                       summary=f"场次 {session_id} 取消：{reason}，释放 {freed} 个座位",
                       affected_schools=schools, result=result)
            self._supersede(actor, targets, "场次已取消，原通知失效")
            for school_id in schools:
                self._notify(actor, school_id, "session_cancelled",
                             f"场次 {session_id} 已取消：{reason}",
                             {"session_id": session_id, "window_id": sess["window_id"]})
            self._notify(actor, None, "session_cancelled",
                         f"场次 {session_id} 已取消（{reason}），涉及 {len(schools)} 所学校，释放 {freed} 座",
                         {"session_id": session_id})
            return result, False

    def shrink_window(self, actor, window_id: str, payload: dict, request_id: str | None = None,
                      occurred_at: str | None = None):
        """直播窗口缩短/容量收缩。超容时须显式选择处置方式，否则返回影响面。"""
        with self._lock:
            hit = self._replay(request_id)
            if hit is not None:
                return hit, True
            window = self.windows.get(window_id)
            if not window:
                raise DomainError("window_not_found", f"窗口 {window_id} 未登记", 404)
            self._require_version(payload.get("expected_version"))
            new_end = payload.get("new_end")
            new_cap = payload.get("new_seat_capacity")
            if new_end is None and new_cap is None:
                raise DomainError("invalid_field", "new_end 与 new_seat_capacity 至少提供一项")
            old_end, old_cap = window["end"], window["seat_capacity"]
            if new_end is not None:
                _parse_time(new_end, "new_end")
                if not (_parse_time(window["start"], "start") < _parse_time(new_end, "new_end") < _parse_time(old_end, "end")):
                    raise DomainError("not_a_shrink", "new_end 必须早于原结束时间且晚于开始时间")
            else:
                new_end = old_end
            if new_cap is not None:
                if isinstance(new_cap, bool) or not isinstance(new_cap, int) or new_cap < 1:
                    raise DomainError("invalid_field", "new_seat_capacity 必须是不小于 1 的整数")
                if new_cap >= old_cap:
                    raise DomainError("not_a_shrink", "new_seat_capacity 必须小于原容量")
            else:
                new_cap = old_cap
            used = self._window_usage(window_id)
            overflow = max(0, used - new_cap)
            resolution = payload.get("resolution")
            if overflow and resolution != "auto_trim":
                candidates = self._trim_candidates(window_id)
                raise DomainError(
                    "capacity_overflow",
                    f"窗口 {window_id} 容量 {old_cap}→{new_cap}，当前已占位 {used}，超出 {overflow} 座；"
                    f"需确认处置方式后重试",
                    409,
                    {"impact": {"window_id": window_id, "used_seats": used,
                                "new_seat_capacity": new_cap, "overflow_seats": overflow,
                                "candidate_drops": candidates},
                     "options": [{"resolution": "auto_trim",
                                  "effect": "按终审通过时间从新到旧移除占位，直至满足容量"}],
                     "recovery": "确认影响面后，以 resolution=auto_trim 重试本请求"})
            # 计算移除方案（确定性：候选有序，覆盖超出量即停）
            drops, covered = [], 0
            if overflow:
                for cand in self._trim_candidates(window_id):
                    if covered >= overflow:
                        break
                    drops.append(cand)
                    covered += cand["seats"]
            dropped_schools = sorted({d["school_id"] for d in drops})
            remaining_schools = sorted(
                {a["school_id"] for s in self.sessions.values()
                 if s["window_id"] == window_id and s["status"] == "locked"
                 for a in s["assignments"]} - set(dropped_schools))
            supersede_targets = self._pending_notifications(
                lambda n: n["kind"] == "session_locked"
                and any(n["school_id"] == d["school_id"] and n["refs"].get("session_id") == d["session_id"]
                        for d in drops))
            superseded_ids = [n["notification_id"] for n in supersede_targets]
            dropped_seats = sum(d["seats"] for d in drops)
            drops_desc = "、".join(
                f"{d['plan_id']}（{d['school_id']}，{d['seats']} 座）" for d in drops) or "无"
            explanation = (
                f"窗口 {window_id} 容量 {old_cap}→{new_cap}、结束时间 {old_end}→{new_end}；"
                f"已占位 {used} 座，超出 {overflow} 座。"
                f"按终审通过时间从新到旧移除 {len(drops)} 条占位共 {dropped_seats} 座：{drops_desc}；"
                f"其余 {len(remaining_schools)} 所学校保留并收到窗口变更通知。"
            )
            version = self._next_version() + len(drops)
            result = {"window_id": window_id, "version": version,
                      "new_end": new_end, "new_seat_capacity": new_cap,
                      "dropped": drops, "used_seats_after": used - dropped_seats,
                      "superseded_notifications": superseded_ids,
                      "notification_scope": {"schools": sorted(dropped_schools + remaining_schools),
                                             "control": True},
                      "notifications_queued": len(dropped_schools) + len(remaining_schools) + 1,
                      "explanation": explanation}
            self._emit(actor, "window_changed",
                       {"window_id": window_id, "old_end": old_end, "new_end": new_end,
                        "old_seat_capacity": old_cap, "new_seat_capacity": new_cap},
                       request_id, occurred_at, schedule=True,
                       summary=f"窗口 {window_id} 收缩：容量 {old_cap}→{new_cap}，结束 {old_end}→{new_end}",
                       affected_schools=sorted(dropped_schools + remaining_schools),
                       result=result)
            for d in drops:
                self._emit(actor, "assignment_removed",
                           {"session_id": d["session_id"], "plan_id": d["plan_id"],
                            "school_id": d["school_id"], "seats": d["seats"],
                            "reason": "窗口容量收缩自动调整"},
                           schedule=True,
                           summary=f"学校 {d['school_id']} 的 {d['seats']} 个座位被移除"
                                   f"（窗口 {window_id} 容量收缩）",
                           affected_schools=[d["school_id"]])
            self._supersede(actor, supersede_targets, "窗口收缩，原场次占位通知失效")
            for school_id in dropped_schools:
                seats = sum(d["seats"] for d in drops if d["school_id"] == school_id)
                self._notify(actor, school_id, "assignment_removed",
                             f"因窗口 {window_id} 容量收缩（{old_cap}→{new_cap}），"
                             f"贵校的 {seats} 个座位已被移除",
                             {"window_id": window_id})
            for school_id in remaining_schools:
                self._notify(actor, school_id, "window_changed",
                             f"窗口 {window_id} 已调整：容量 {old_cap}→{new_cap}，"
                             f"结束时间 {old_end}→{new_end}，贵校占位保留",
                             {"window_id": window_id})
            self._notify(actor, None, "window_changed", explanation, {"window_id": window_id})
            return result, False

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def dispatch_notifications(self, actor):
        """下发全部待发送通知；已失效（superseded）的永不下发。天然幂等。"""
        with self._lock:
            pending = [n for n in self.notifications.values() if n["status"] == "pending"]
            superseded = [n["notification_id"] for n in self.notifications.values()
                          if n["status"] == "superseded"]
            for n in pending:
                self._emit(actor, "notification_dispatched",
                           {"notification_id": n["notification_id"]})
            return {"dispatched": [n["notification_id"] for n in pending],
                    "dispatched_count": len(pending),
                    "skipped_superseded": superseded}, False

    def list_notifications(self, actor, status: str | None = None, school_id: str | None = None):
        with self._lock:
            if actor.role == "school":
                if school_id is not None and school_id != actor.school_id:
                    raise DomainError("forbidden", "学校只能查看本校通知", 403)
                school_id = actor.school_id
            items = []
            for n in self.notifications.values():
                if status and n["status"] != status:
                    continue
                if school_id is not None and n["school_id"] != school_id:
                    continue
                if school_id is None and actor.role != "control":
                    continue
                items.append(dict(n))
            return {"notifications": items}

    # ------------------------------------------------------------------
    # 查询与版本还原
    # ------------------------------------------------------------------
    def _plan_view(self, plan: dict) -> dict:
        return {
            "plan_id": plan["plan_id"], "school_id": plan["school_id"],
            "window_id": plan["window_id"], "student_count": plan["student_count"],
            "status": plan["status"], "review_chain": list(plan["review_chain"]),
            "session_id": plan["session_id"], "submitted_at": plan["submitted_at"],
            "approved_at": plan["approved_at"], "withdrawn_reason": plan["withdrawn_reason"],
            "students_recorded": len(self.students.get(plan["plan_id"], [])),
        }

    def _session_view(self, sess: dict) -> dict:
        return {
            "session_id": sess["session_id"], "window_id": sess["window_id"],
            "status": sess["status"], "seats_total": sum(a["seats"] for a in sess["assignments"]),
            "assignments": [dict(a) for a in sess["assignments"]],
            "locked_at": sess["locked_at"], "cancelled_reason": sess["cancelled_reason"],
        }

    def _schedule_snapshot(self) -> dict:
        windows = []
        for w in sorted(self.windows.values(), key=lambda x: x["window_id"]):
            used = self._window_usage(w["window_id"])
            windows.append({**w, "used_seats": used,
                            "remaining_seats": w["seat_capacity"] - used})
        return {
            "version": self.schedule_version,
            "windows": windows,
            "sessions": [self._session_view(s) for s in
                         sorted(self.sessions.values(), key=lambda x: x["session_id"])],
        }

    def schedule(self) -> dict:
        with self._lock:
            return self._schedule_snapshot()

    def schedule_versions(self) -> dict:
        with self._lock:
            return {"current_version": self.schedule_version,
                    "versions": list(self.schedule_log)}

    def schedule_at(self, version: int) -> dict:
        """还原指定排期版本：重放事件至该版本对应的最后一条事件为止。"""
        with self._lock:
            if isinstance(version, bool) or not isinstance(version, int) \
                    or version < 1 or version > self.schedule_version:
                raise DomainError("version_not_found",
                                  f"排期版本 v{version} 不存在（当前 v{self.schedule_version}）", 404)
            scratch = object.__new__(Domain)
            scratch._reset()
            for event in self.store.events:
                scratch._project(event)
                if event.get("version") == version:
                    break
            return scratch._schedule_snapshot()

    def get_plan(self, plan_id: str) -> dict:
        with self._lock:
            plan = self.plans.get(plan_id)
            if not plan:
                raise DomainError("plan_not_found", f"计划 {plan_id} 不存在", 404)
            return self._plan_view(plan)

    def list_plans(self, school_id=None, status=None, window_id=None) -> dict:
        with self._lock:
            items = [self._plan_view(p) for p in self.plans.values()
                     if (school_id is None or p["school_id"] == school_id)
                     and (status is None or p["status"] == status)
                     and (window_id is None or p["window_id"] == window_id)]
            return {"plans": items}

    def list_schools(self) -> dict:
        with self._lock:
            return {"schools": [dict(s) for s in self.schools.values()]}

    def list_windows(self) -> dict:
        with self._lock:
            return {"windows": [dict(w) for w in self.windows.values()]}

    def list_sessions(self) -> dict:
        with self._lock:
            return {"sessions": [self._session_view(s) for s in self.sessions.values()]}

    def events_since(self, actor, since: int) -> dict:
        """断线重连：返回 since 之后的事件与最新 seq（客户端的最后确认点）。"""
        with self._lock:
            events, latest = self.store.since(since)
            if actor.role == "auditor":
                events = [self._redacted(e) for e in events]
            return {"events": events, "latest_seq": latest}

    @staticmethod
    def _redacted(event: dict) -> dict:
        """审计员视角：学生名单脱敏。"""
        if event["type"] != "students_recorded":
            return event
        redacted = dict(event)
        payload = dict(event["payload"])
        payload["count"] = len(payload.get("students", []))
        payload["students"] = []
        payload["redacted"] = True
        redacted["payload"] = payload
        return redacted

    def audit_log(self) -> dict:
        with self._lock:
            return {"audit": list(self.audit_trail)}
