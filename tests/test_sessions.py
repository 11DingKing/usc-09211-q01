"""场次锁定与容量约束测试。"""
import unittest

from tests.helpers import CONTROL, Server, schedule_version, setup_plan


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.api = self.server.__enter__()

    def tearDown(self):
        self.server.__exit__()

    def test_lock_session_and_schedule_view(self):
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-1", "seats": 30}],
            "expected_version": 0}, CONTROL)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["seats_total"], 30)
        self.assertEqual(body["notification_scope"]["schools"], ["sch-1"])

        st, body, _ = self.api.get("/schedule", CONTROL)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["windows"][0]["used_seats"], 30)
        self.assertEqual(body["windows"][0]["remaining_seats"], 70)
        self.assertEqual(body["sessions"][0]["status"], "locked")

    def test_lock_requires_approved_plan(self):
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        st, body, _ = self.api.post("/schools", {
            "school_id": "sch-2", "name": "澳门培正", "region": "澳门",
            "seat_capacity": 40}, CONTROL)
        self.assertEqual(st, 200, body)
        st, body, _ = self.api.post("/plans", {
            "plan_id": "plan-2", "school_id": "sch-2", "window_id": "win-1",
            "student_count": 10}, CONTROL)
        self.assertEqual(st, 200, body)
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-2", "seats": 10}]}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "plan_not_approved")
        self.assertEqual(body["current_status"], "submitted")

    def test_window_capacity_exceeded(self):
        setup_plan(self.api, "sch-1", "plan-1", students=60, school_cap=80)
        setup_plan(self.api, "sch-2", "plan-2", students=60, school_cap=80, region="澳门")
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-1", "seats": 60}]}, CONTROL)
        self.assertEqual(st, 200, body)
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-2", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-2", "seats": 60}]}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "capacity_exceeded")
        self.assertEqual(body["remaining_seats"], 40)
        self.assertEqual(body["requested_seats"], 60)

    def test_seats_exceed_plan(self):
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-1", "seats": 31}]}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "capacity_exceeded")

    def test_plan_already_assigned(self):
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-1", "seats": 30}]}, CONTROL)
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-2", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-1", "seats": 30}]}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "plan_already_assigned")
        self.assertEqual(body["session_id"], "sess-1")

    def test_stale_expected_version_conflict_is_explainable(self):
        """两地同时改动：过期版本被拒，且返回期间变更说明。"""
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        setup_plan(self.api, "sch-2", "plan-2", students=20, region="澳门")
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-1", "seats": 30}],
            "expected_version": 0}, CONTROL)
        self.assertEqual(st, 200, body)
        # 另一方仍基于 v0 操作
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-2", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-2", "seats": 20}],
            "expected_version": 0}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "version_conflict")
        self.assertEqual(body["expected_version"], 0)
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(len(body["intervening"]), 1)
        self.assertIn("sess-1", body["intervening"][0]["summary"])
        self.assertIn("recovery", body)
        # 基于最新版本重试成功
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-2", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-2", "seats": 20}],
            "expected_version": schedule_version(self.api)}, CONTROL)
        self.assertEqual(st, 200, body)

    def test_window_overlap_rejected(self):
        setup_plan(self.api, "sch-1", "plan-1", students=10)
        st, body, _ = self.api.post("/windows", {
            "window_id": "win-2", "start": "2026-10-01T10:30:00+08:00",
            "end": "2026-10-01T12:00:00+08:00", "seat_capacity": 50}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "window_overlap")
        self.assertEqual(body["conflict_with"], "win-1")

    def test_lock_idempotent_replay(self):
        setup_plan(self.api, "sch-1", "plan-1", students=30)
        payload = {"session_id": "sess-1", "window_id": "win-1",
                   "assignments": [{"plan_id": "plan-1", "seats": 30}]}
        st1, body1, _ = self.api.post("/sessions", payload, CONTROL, idem="lock-1")
        st2, body2, h2 = self.api.post("/sessions", payload, CONTROL, idem="lock-1")
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(body1, body2)
        self.assertEqual(h2.get("idempotent-replay"), "true")
        st, body, _ = self.api.get("/schedule", CONTROL)
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["sessions"]), 1)


if __name__ == "__main__":
    unittest.main()
