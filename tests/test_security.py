"""未成年人资料隔离与访问审计测试。"""
import unittest

from tests.helpers import AUDITOR, CONTROL, REVIEWER, Server, school, setup_plan

STUDENTS = [
    {"student_id": "st-1", "name": "陈小明", "grade": "中四"},
    {"student_id": "st-2", "name": "林嘉欣", "grade": "中五"},
]


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.api = self.server.__enter__()
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        setup_plan(self.api, "sch-2", "plan-2", students=20, region="澳门")
        st, body, _ = self.api.put("/plans/plan-1/students",
                                   {"students": STUDENTS}, school("sch-1"))
        assert st == 200, body

    def tearDown(self):
        self.server.__exit__()

    def test_unauthenticated_rejected(self):
        st, body, _ = self.api.call("GET", "/plans")
        self.assertEqual(st, 401)
        st, body, _ = self.api.call("GET", "/plans", headers={
            "X-Actor-Id": "x", "X-Actor-Role": "superadmin"})
        self.assertEqual(st, 401)

    def test_student_data_role_isolation(self):
        cases = [
            ("本校可读", school("sch-1"), 200),
            ("他校不可读", school("sch-2"), 403),
            ("审核员不可读", REVIEWER, 403),
            ("审计员不可读", AUDITOR, 403),
            ("总控可读", CONTROL, 200),
        ]
        for name, headers, expected in cases:
            with self.subTest(name=name):
                st, body, _ = self.api.get(
                    "/plans/plan-1/students?purpose=直播联排核对", headers)
                self.assertEqual(st, expected, body)
                if expected == 200:
                    self.assertEqual(len(body["students"]), 2)

    def test_purpose_required(self):
        st, body, _ = self.api.get("/plans/plan-1/students", CONTROL)
        self.assertEqual(st, 400)
        self.assertEqual(body["error"], "purpose_required")

    def test_access_audit_trail(self):
        self.api.get("/plans/plan-1/students?purpose=直播联排核对", CONTROL)
        self.api.get("/plans/plan-1/students?purpose=出勤统计", school("sch-1"))
        # 被拒的访问也应有记录？——仅成功访问写入审计；确认审计内容
        st, body, _ = self.api.get("/audit", AUDITOR)
        self.assertEqual(st, 200)
        entries = [a for a in body["audit"] if a["plan_id"] == "plan-1"]
        self.assertEqual(len(entries), 2)
        self.assertEqual({e["actor_id"] for e in entries}, {"ops-1", "teacher-sch-1"})
        self.assertEqual({e["purpose"] for e in entries}, {"直播联排核对", "出勤统计"})
        self.assertTrue(all(e["seq"] for e in entries))
        # 学校角色不能读审计
        st, _, _ = self.api.get("/audit", school("sch-1"))
        self.assertEqual(st, 403)

    def test_auditor_event_stream_is_redacted(self):
        st, body, _ = self.api.get("/events", AUDITOR)
        self.assertEqual(st, 200)
        student_events = [e for e in body["events"] if e["type"] == "students_recorded"]
        self.assertEqual(len(student_events), 1)
        self.assertEqual(student_events[0]["payload"]["students"], [])
        self.assertTrue(student_events[0]["payload"]["redacted"])
        self.assertEqual(student_events[0]["payload"]["count"], 2)
        # 总控可见完整事件
        st, body, _ = self.api.get("/events", CONTROL)
        full = [e for e in body["events"] if e["type"] == "students_recorded"][0]
        self.assertEqual(len(full["payload"]["students"]), 2)

    def test_record_students_validation(self):
        st, body, _ = self.api.put("/plans/plan-1/students", {"students": []}, school("sch-1"))
        self.assertEqual(st, 400)
        st, body, _ = self.api.put("/plans/plan-1/students", {
            "students": [{"student_id": "x", "name": "甲", "grade": "中一"},
                         {"student_id": "x", "name": "乙", "grade": "中一"}]}, school("sch-1"))
        self.assertEqual(st, 400)
        # 他校不能登记
        st, body, _ = self.api.put("/plans/plan-1/students",
                                   {"students": [{"student_id": "y", "name": "丙", "grade": "中二"}]},
                                   school("sch-2"))
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
