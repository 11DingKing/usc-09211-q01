"""HTTP 接口层：JSON 路由、身份解析、错误映射。

所有变更类接口接受 Idempotency-Key 请求头：断线重试同一键返回原结果，
响应头 Idempotent-Replay: true 标识这是一次幂等重放。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .auth import actor_from_headers, AuthError
from .domain import DomainError


def _idem(headers) -> str | None:
    key = headers.get("Idempotency-Key")
    return key.strip() if key and key.strip() else None


def _cmd(result_tuple):
    """命令结果 -> (status, body, headers)。"""
    result, replayed = result_tuple
    return 200, result, ({"Idempotent-Replay": "true"} if replayed else {})


# ----------------------------------------------------------------------
# 路由处理函数
# ----------------------------------------------------------------------
def _register_school(domain, actor, params, body, query, headers):
    return _cmd(domain.register_school(actor, body, _idem(headers), body.get("occurred_at")))


def _list_schools(domain, actor, params, body, query, headers):
    return 200, domain.list_schools(), {}


def _register_window(domain, actor, params, body, query, headers):
    return _cmd(domain.register_window(actor, body, _idem(headers), body.get("occurred_at")))


def _list_windows(domain, actor, params, body, query, headers):
    return 200, domain.list_windows(), {}


def _submit_plan(domain, actor, params, body, query, headers):
    return _cmd(domain.submit_plan(actor, body, _idem(headers), body.get("occurred_at")))


def _list_plans(domain, actor, params, body, query, headers):
    return 200, domain.list_plans(query.get("school_id"), query.get("status"), query.get("window_id")), {}


def _get_plan(domain, actor, params, body, query, headers):
    return 200, domain.get_plan(params["plan_id"]), {}


def _review_plan(domain, actor, params, body, query, headers):
    return _cmd(domain.review_plan(actor, params["plan_id"], body, _idem(headers), body.get("occurred_at")))


def _withdraw_plan(domain, actor, params, body, query, headers):
    return _cmd(domain.withdraw_plan(actor, params["plan_id"], body, _idem(headers), body.get("occurred_at")))


def _record_students(domain, actor, params, body, query, headers):
    return _cmd(domain.record_students(actor, params["plan_id"], body, _idem(headers), body.get("occurred_at")))


def _get_students(domain, actor, params, body, query, headers):
    return 200, domain.get_students(actor, params["plan_id"], query.get("purpose")), {}


def _lock_session(domain, actor, params, body, query, headers):
    return _cmd(domain.lock_session(actor, body, _idem(headers), body.get("occurred_at")))


def _list_sessions(domain, actor, params, body, query, headers):
    return 200, domain.list_sessions(), {}


def _cancel_session(domain, actor, params, body, query, headers):
    return _cmd(domain.cancel_session(actor, params["session_id"], body, _idem(headers), body.get("occurred_at")))


def _shrink_window(domain, actor, params, body, query, headers):
    return _cmd(domain.shrink_window(actor, params["window_id"], body, _idem(headers), body.get("occurred_at")))


def _schedule(domain, actor, params, body, query, headers):
    return 200, domain.schedule(), {}


def _versions(domain, actor, params, body, query, headers):
    return 200, domain.schedule_versions(), {}


def _version_at(domain, actor, params, body, query, headers):
    return 200, domain.schedule_at(int(params["version"])), {}


def _events(domain, actor, params, body, query, headers):
    try:
        since = int(query.get("since", "0"))
    except ValueError:
        raise DomainError("invalid_field", "since 必须是整数")
    if since < 0:
        raise DomainError("invalid_field", "since 不能为负数")
    return 200, domain.events_since(actor, since), {}


def _notifications(domain, actor, params, body, query, headers):
    return 200, domain.list_notifications(actor, query.get("status"), query.get("school_id")), {}


def _dispatch(domain, actor, params, body, query, headers):
    result, _ = domain.dispatch_notifications(actor)
    return 200, result, {}


def _audit(domain, actor, params, body, query, headers):
    return 200, domain.audit_log(), {}


ROUTES = [
    ("POST", re.compile(r"/schools"), ("control",), _register_school),
    ("GET", re.compile(r"/schools"), None, _list_schools),
    ("POST", re.compile(r"/windows"), ("control",), _register_window),
    ("GET", re.compile(r"/windows"), None, _list_windows),
    ("POST", re.compile(r"/plans"), ("school", "control"), _submit_plan),
    ("GET", re.compile(r"/plans"), None, _list_plans),
    ("GET", re.compile(r"/plans/(?P<plan_id>[^/]+)"), None, _get_plan),
    ("POST", re.compile(r"/plans/(?P<plan_id>[^/]+)/reviews"), ("reviewer", "control"), _review_plan),
    ("POST", re.compile(r"/plans/(?P<plan_id>[^/]+)/withdraw"), ("school", "control"), _withdraw_plan),
    ("PUT", re.compile(r"/plans/(?P<plan_id>[^/]+)/students"), ("school", "control"), _record_students),
    ("GET", re.compile(r"/plans/(?P<plan_id>[^/]+)/students"), ("school", "control"), _get_students),
    ("POST", re.compile(r"/sessions"), ("control",), _lock_session),
    ("GET", re.compile(r"/sessions"), None, _list_sessions),
    ("POST", re.compile(r"/sessions/(?P<session_id>[^/]+)/cancel"), ("control",), _cancel_session),
    ("POST", re.compile(r"/windows/(?P<window_id>[^/]+)/shrink"), ("control",), _shrink_window),
    ("GET", re.compile(r"/schedule"), None, _schedule),
    ("GET", re.compile(r"/schedule/versions"), None, _versions),
    ("GET", re.compile(r"/schedule/versions/(?P<version>\d+)"), None, _version_at),
    ("GET", re.compile(r"/events"), ("control", "auditor"), _events),
    ("GET", re.compile(r"/notifications"), ("control", "school"), _notifications),
    ("POST", re.compile(r"/notifications/dispatch"), ("control",), _dispatch),
    ("GET", re.compile(r"/audit"), ("control", "auditor"), _audit),
]


def make_handler(domain):
    """构造绑定指定领域实例的请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def log_message(self, format, *args):
            return

        # ---------------- 内部 ----------------
        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path, query = parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if path == "/health" and method == "GET":
                return self._respond(200, {"status": "ok"})
            try:
                actor = actor_from_headers(self.headers)
            except AuthError as exc:
                return self._respond(401, {"error": "unauthenticated", "message": str(exc)})
            for m, pattern, roles, fn in ROUTES:
                if m != method:
                    continue
                match = pattern.fullmatch(path)
                if not match:
                    continue
                if roles and actor.role not in roles:
                    return self._respond(403, {
                        "error": "forbidden",
                        "message": f"该操作需要角色: {'/'.join(roles)}",
                    })
                try:
                    body = self._json_body() if method in ("POST", "PUT") else {}
                    status, obj, extra = fn(domain, actor, match.groupdict(), body, query, self.headers)
                    return self._respond(status, obj, extra)
                except DomainError as exc:
                    return self._respond(exc.status, exc.body())
                except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                    return self._respond(500, {"error": "internal", "message": str(exc)})
            return self._respond(404, {"error": "not_found", "message": f"{method} {path} 不存在"})

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise DomainError("invalid_json", "请求体不是合法 JSON", 400)
            if not isinstance(data, dict):
                raise DomainError("invalid_json", "请求体必须是 JSON 对象", 400)
            return data

        def _respond(self, status, obj, extra_headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler
