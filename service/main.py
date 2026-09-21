"""天地课堂联播台 HTTP 入口（仅依赖标准库）。

鉴权约定（演示环境由网关/调用方注入请求头）：
- X-Actor-Id：操作人稳定标识
- X-Actor-Role：control / school / auditor
- X-Actor-School：学校角色所属学校标识
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .eventstore import EventStore
from .models import ApiError, Student
from .service import Actor, ApplicationService, _session_to_dict


def build_application(event_log_path: Optional[str] = None) -> ApplicationService:
    return ApplicationService(EventStore(event_log_path))


# 默认内存实例；设置环境变量 TIAN_DI_EVENT_LOG 可落盘并在重启后重放还原。
APP = build_application(os.environ.get("TIAN_DI_EVENT_LOG"))


class Handler(BaseHTTPRequestHandler):
    app = APP

    # ------------------------------------------------------------ 基础
    def log_message(self, format, *args):
        return

    def _actor(self) -> Actor:
        actor_id = self.headers.get("X-Actor-Id")
        role = self.headers.get("X-Actor-Role", "")
        school_id = self.headers.get("X-Actor-School")
        if not actor_id:
            raise ApiError(401, "UNAUTHENTICATED", "缺少 X-Actor-Id 请求头")
        if role not in ("control", "school", "auditor"):
            raise ApiError(403, "FORBIDDEN", "X-Actor-Role 非法")
        return Actor(id=actor_id, role=role, school_id=school_id)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_BODY", "请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _query(self) -> dict:
        return parse_qs(urlsplit(self.path).query)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            actor = self._actor()
            body = self._read_json() if method == "POST" else {}
            route, kwargs = self._match(method, path)
            if route is None:
                raise ApiError(404, "NOT_FOUND", f"无此路由：{method} {path}")
            result = route(actor, body, **kwargs)
            self._send(200 if result is not None else 204, result or {})
        except ApiError as err:
            self._send(err.status, err.to_body())
        except Exception as err:  # noqa: BLE001 - 边界统一兜底
            self._send(500, {"error": "INTERNAL", "message": str(err)})

    # ------------------------------------------------------------ 路由
    def _match(self, method: str, path: str):
        q = self._query
        app = self.app
        if method == "GET":
            if path == "/schedule":
                return (lambda a, b: app.schedule_view()), {}
            m = re.fullmatch(r"/schedule/versions/(\d+)", path)
            if m:
                return (lambda a, b, n: app.version_view(int(n))), {"n": m.group(1)}
            if path == "/conflicts":
                return self._list_conflicts, {}
            if path == "/notices":
                return self._list_notices, {}
            if path == "/students":
                school_id = q().get("school_id", [""])[0]
                return (lambda a, b: [s.__dict__ for s in
                                      app.list_students(a, school_id)]), {}
            if path == "/audit":
                return self._audit_trail, {}
            if path == "/sync":
                after = int(q().get("after_seq", ["0"])[0])
                return (lambda a, b: app.sync(a, after)), {}
            m = re.fullmatch(r"/plans/([^/]+)", path)
            if m:
                return (lambda a, b, pid: app.plan_view(a, pid)), {"pid": m.group(1)}
        if method == "POST":
            if path == "/admin/schools":
                return self._register_school, {}
            if path == "/admin/campuses":
                return self._register_campus, {}
            if path == "/admin/windows":
                return self._add_window, {}
            if path == "/students":
                return self._add_student, {}
            if path == "/plans":
                return self._submit_plan, {}
            if path == "/notices/dispatch":
                return (lambda a, b: app.dispatch_notices(a)), {}
            m = re.fullmatch(r"/plans/([^/]+)/approvals", path)
            if m:
                return self._approve, {"pid": m.group(1)}
            m = re.fullmatch(r"/plans/([^/]+)/withdraw", path)
            if m:
                return self._withdraw, {"pid": m.group(1)}
            m = re.fullmatch(r"/plans/([^/]+)/hold", path)
            if m:
                return self._hold_plan, {"pid": m.group(1)}
            m = re.fullmatch(r"/windows/([^/]+)/lock", path)
            if m:
                return self._lock_window, {"wid": m.group(1)}
            m = re.fullmatch(r"/windows/([^/]+)/revise", path)
            if m:
                return self._revise_window, {"wid": m.group(1)}
            m = re.fullmatch(r"/conflicts/([^/]+)/resolve", path)
            if m:
                return self._resolve_conflict, {"cid": m.group(1)}
        return None, {}

    # ------------------------------------------------------------ 处理器
    def _register_school(self, actor, body):
        school = self.app.register_school(
            actor, body["id"], body["name"], body["region"])
        return school.__dict__

    def _register_campus(self, actor, body):
        campus = self.app.register_campus(
            actor, body["id"], body["school_id"], int(body["venue_capacity"]))
        return campus.__dict__

    def _add_window(self, actor, body):
        window = self.app.add_window(
            actor, body["id"], body["campus_id"], body["start"], body["end"],
            int(body["signal_capacity"]))
        return window.__dict__

    def _add_student(self, actor, body):
        student = Student(id=body["id"], school_id=body["school_id"],
                          name=body["name"], grade=body.get("grade", ""),
                          contact=body["contact"])
        return self.app.add_student(actor, student).__dict__

    def _submit_plan(self, actor, body):
        plan = self.app.submit_plan(
            actor, body["id"], body["window_id"], int(body["seats"]),
            list(body.get("student_ids", [])), list(body.get("questions", [])),
            list(body.get("materials", [])), body["idempotency_key"],
            event_time=body.get("event_time"))
        return self.app.plan_view(actor, plan.id)

    def _approve(self, actor, body, pid):
        plan = self.app.approve_or_reject(
            actor, pid, body["action"], body.get("note", ""))
        return self.app.plan_view(actor, plan.id)

    def _withdraw(self, actor, body, pid):
        plan = self.app.withdraw_plan(
            actor, pid, body.get("reason", ""),
            expected_version=body.get("expected_version"))
        return self.app.plan_view(actor, plan.id)

    def _hold_plan(self, actor, _body, pid):
        hold = self.app.hold_plan(actor, pid)
        return {"id": hold.id, "plan_id": hold.plan_id, "window_id": hold.window_id,
                "school_id": hold.school_id, "seats": hold.seats,
                "status": hold.status.value}

    def _lock_window(self, actor, body, wid):
        session = self.app.lock_window(actor, wid, body.get("reason", ""))
        return _session_to_dict(session)

    def _revise_window(self, actor, body, wid):
        return self.app.revise_window(
            actor, wid, body["start"], body["end"],
            int(body["signal_capacity"]), body.get("reason", ""),
            body.get("expected_version"))

    def _resolve_conflict(self, actor, body, cid):
        return self.app.resolve_conflict(actor, cid, body["choice"])

    def _list_conflicts(self, actor, _body):
        return [self.app.conflict_view(c) for c in self.app.conflicts.values()]

    def _list_notices(self, actor, _body):
        return [self.app.notice_view(n) for n in self.app.notices.values()]

    def _audit_trail(self, actor, _body):
        if actor.role not in ("control", "auditor"):
            raise ApiError(403, "FORBIDDEN", "审计日志仅总控/审计员可查")
        return list(self.app.audit)


def run(host: str = "127.0.0.1", port: int = 8000):
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    run()
