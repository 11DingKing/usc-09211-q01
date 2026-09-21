"""HTTP 端到端验收：完整跑通上报→审核→占位→锁定→缩短→处置→通知。"""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.eventstore import EventStore
from service.main import Handler
from service.service import ApplicationService


def _app():
    return ApplicationService(EventStore())


class ApiServer:
    def __init__(self):
        Handler.app = ApplicationService(EventStore())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, *, actor="ctrl-1", role="control",
             school=None):
        conn = HTTPConnection("127.0.0.1", self.port)
        headers = {"X-Actor-Id": actor, "X-Actor-Role": role}
        if school:
            headers["X-Actor-School"] = school
        payload = b""
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        return resp.status, data


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiServer()

    def tearDown(self):
        self.api.stop()

    def test_full_broadcast_flow(self):
        call = self.api.call
        # 总控登记两地学校、校区与窗口（校区容量 5）。
        self.assertEqual(call("POST", "/admin/schools",
                              {"id": "school-A", "name": "港校", "region": "HK"})[0], 200)
        self.assertEqual(call("POST", "/admin/schools",
                              {"id": "school-B", "name": "澳校", "region": "MO"})[0], 200)
        self.assertEqual(call("POST", "/admin/campuses",
                              {"id": "campus-A", "school_id": "school-A",
                               "venue_capacity": 5})[0], 200)
        self.assertEqual(call("POST", "/admin/windows",
                              {"id": "w1", "campus_id": "campus-A",
                               "start": "2026-10-01T09:00:00Z",
                               "end": "2026-10-01T09:20:00Z",
                               "signal_capacity": 5})[0], 200)

        # A 校登记学生并上报 3 席计划（含提问与课后材料）。
        for i in range(3):
            status, _ = call("POST", "/students",
                             {"id": f"a-{i}", "school_id": "school-A",
                              "name": f"甲{i}", "grade": "初一",
                              "contact": f"a{i}@p.test"},
                             actor="u-a", role="school", school="school-A")
            self.assertEqual(status, 200)
        plan_body = {
            "id": "plan-A", "window_id": "w1", "seats": 3,
            "student_ids": [f"a-{i}" for i in range(3)],
            "questions": [{"id": "q1", "text": "能看到地球吗？", "student_id": "a-0"}],
            "materials": ["after-class-A.pdf"], "idempotency_key": "batch-1",
        }
        status, plan = call("POST", "/plans", plan_body,
                            actor="u-a", role="school", school="school-A")
        self.assertEqual(status, 200)
        self.assertEqual(plan["status"], "SUBMITTED")
        # 重复上报：幂等返回同一计划。
        status, plan_again = call("POST", "/plans", {**plan_body, "id": "plan-A2"},
                                  actor="u-a", role="school", school="school-A")
        self.assertEqual(status, 200)
        self.assertEqual(plan_again["id"], "plan-A")

        # 审核链：校内 → 总控。
        self.assertEqual(call("POST", "/plans/plan-A/approvals",
                              {"action": "approve", "note": "校内通过"},
                              actor="u-a", role="school", school="school-A")[0], 200)
        self.assertEqual(call("POST", "/plans/plan-A/approvals",
                              {"action": "approve", "note": "总控通过"})[0], 200)

        # B 校上报 3 席，占位时第二个学校超额，被明确拒绝（3+3>5）。
        for i in range(3):
            call("POST", "/students",
                 {"id": f"b-{i}", "school_id": "school-B", "name": f"乙{i}",
                  "grade": "初一", "contact": f"b{i}@p.test"},
                 actor="u-b", role="school", school="school-B")
        b_body = {"id": "plan-B", "window_id": "w1", "seats": 3,
                  "student_ids": [f"b-{i}" for i in range(3)],
                  "questions": [], "materials": [], "idempotency_key": "batch-b"}
        call("POST", "/plans", b_body, actor="u-b", role="school", school="school-B")
        call("POST", "/plans/plan-B/approvals", {"action": "approve"},
             actor="u-b", role="school", school="school-B")
        call("POST", "/plans/plan-B/approvals", {"action": "approve"})
        self.assertEqual(call("POST", "/plans/plan-A/hold", {})[0], 200)
        status, err = call("POST", "/plans/plan-B/hold", {})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "CAPACITY_EXCEEDED")

        # 锁定窗口 → v1，确认通知草稿生成。
        status, session = call("POST", "/windows/w1/lock", {"reason": "首期"})
        self.assertEqual(status, 200)
        self.assertEqual(session["allocations"][0]["seats"], 3)

        # 窗口缩短到 2 席 → 409 冲突，给出选项与影响范围。
        status, conflict = call("POST", "/windows/w1/revise", {
            "start": "2026-10-01T09:05:00Z", "end": "2026-10-01T09:15:00Z",
            "signal_capacity": 2, "reason": "过境缩短", "expected_version": 1})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "CONFLICT_OPEN")
        cid = conflict["conflict_id"]

        # 冲突未决前派发：不发送。
        status, out = call("POST", "/notices/dispatch", {})
        self.assertEqual(out["dispatched"], [])

        # 处置：取消场次；随后派发，A 校学生收到取消通知。
        self.assertEqual(call("POST", f"/conflicts/{cid}/resolve",
                              {"choice": "CANCEL_SESSION"})[0], 200)
        status, out = call("POST", "/notices/dispatch", {})
        self.assertEqual(status, 200)
        recipients = {r for item in out["dispatched"] for r in item["recipients"]}
        self.assertIn("a0@p.test", recipients)

        # 排期版本可查，审计可查，隔离生效。
        status, v1 = call("GET", "/schedule/versions/1")
        self.assertEqual(status, 200)
        self.assertEqual(v1["version"], 1)
        status, audit = call("GET", "/audit")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["action"] == "resolve_conflict" for a in audit))
        status, err = call("GET", "/students?school_id=school-A",
                           actor="u-b", role="school", school="school-B")
        self.assertEqual(status, 403)

    def test_health_and_auth(self):
        status, body = self.api.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        conn = HTTPConnection("127.0.0.1", self.api.port)
        conn.request("GET", "/schedule")
        self.assertEqual(conn.getresponse().status, 401)


if __name__ == "__main__":
    unittest.main()
