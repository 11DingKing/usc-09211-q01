"""测试公共工具：内存/临时目录服务、HTTP 客户端、常用业务前置。"""
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from service.domain import Domain
from service.httpapi import make_handler
from service.store import EventStore

CONTROL = {"X-Actor-Id": "ops-1", "X-Actor-Role": "control"}
REVIEWER = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}
AUDITOR = {"X-Actor-Id": "aud-1", "X-Actor-Role": "auditor"}

WINDOW = {
    "window_id": "win-1",
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T11:00:00+08:00",
    "seat_capacity": 100,
}


def school(school_id):
    return {"X-Actor-Id": f"teacher-{school_id}", "X-Actor-Role": "school",
            "X-School-Id": school_id}


class Api:
    """测试用 HTTP 客户端。"""

    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request(method, quote(path, safe="/?=&%"),
                     body=json.dumps(body).encode() if body is not None else None,
                     headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        obj = json.loads(raw) if raw else None
        out_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, obj, out_headers

    def get(self, path, headers=CONTROL):
        return self.call("GET", path, headers=headers)

    def post(self, path, body=None, headers=CONTROL, idem=None):
        hdrs = dict(headers)
        if idem:
            hdrs["Idempotency-Key"] = idem
        return self.call("POST", path, body=body, headers=hdrs)

    def put(self, path, body=None, headers=CONTROL, idem=None):
        hdrs = dict(headers)
        if idem:
            hdrs["Idempotency-Key"] = idem
        return self.call("PUT", path, body=body, headers=hdrs)


class Server:
    """绑定随机端口的测试服务；data_dir 提供时启用持久化。"""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir
        store_path = Path(data_dir) / "events.jsonl" if data_dir else None
        self.store = EventStore(store_path)
        self.domain = Domain(self.store)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.domain))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return Api(self.httpd.server_address[1])

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def approve_plan(api, plan_id):
    """走完初审 + 终审。"""
    st, body, _ = api.post(f"/plans/{plan_id}/reviews",
                           {"stage": "initial", "action": "approve", "comment": "材料齐全"},
                           REVIEWER)
    assert st == 200, body
    st, body, _ = api.post(f"/plans/{plan_id}/reviews",
                           {"stage": "final", "action": "approve", "comment": "同意"},
                           CONTROL)
    assert st == 200, body


def setup_plan(api, school_id="sch-1", plan_id="plan-1", students=30,
               school_cap=50, window_cap=100, window_id="win-1", region="香港"):
    """登记学校与窗口、提交并通过计划，返回计划 id。"""
    st, body, _ = api.post("/schools", {
        "school_id": school_id, "name": f"学校{school_id}", "region": region,
        "seat_capacity": school_cap}, CONTROL)
    assert st == 200, body
    st, body, _ = api.get("/windows", CONTROL)
    if window_id not in {w["window_id"] for w in body["windows"]}:
        st, body, _ = api.post("/windows", {
            **WINDOW, "window_id": window_id, "seat_capacity": window_cap}, CONTROL)
        assert st == 200, body
    st, body, _ = api.post("/plans", {
        "plan_id": plan_id, "school_id": school_id, "window_id": window_id,
        "student_count": students}, school(school_id))
    assert st == 200, body
    approve_plan(api, plan_id)
    return plan_id


def schedule_version(api):
    st, body, _ = api.get("/schedule", CONTROL)
    assert st == 200, body
    return body["version"]
