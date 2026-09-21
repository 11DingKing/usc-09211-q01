"""冲突处置测试：窗口收缩、学校退出、场次取消与版本还原。"""
import unittest

from tests.helpers import CONTROL, Server, schedule_version, school, setup_plan


class ConflictTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.api = self.server.__enter__()

    def tearDown(self):
        self.server.__exit__()

    def _two_schools_locked(self):
        """两校各 30 座锁定在同一场次，返回当前版本。"""
        setup_plan(self.api, "sch-a", "plan-a", students=30)
        setup_plan(self.api, "sch-b", "plan-b", students=30, region="澳门")
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-a", "seats": 30},
                            {"plan_id": "plan-b", "seats": 30}],
            "expected_version": 0}, CONTROL)
        self.assertEqual(st, 200, body)
        return body["version"]

    def test_shrink_overflow_requires_resolution(self):
        version = self._two_schools_locked()
        st, body, _ = self.api.post("/windows/win-1/shrink", {
            "new_seat_capacity": 40, "expected_version": version}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "capacity_overflow")
        self.assertEqual(body["impact"]["overflow_seats"], 20)
        self.assertEqual(body["impact"]["used_seats"], 60)
        self.assertEqual(len(body["impact"]["candidate_drops"]), 2)
        self.assertEqual(body["options"][0]["resolution"], "auto_trim")
        # 未确认处置前状态不变
        st, sched, _ = self.api.get("/schedule", CONTROL)
        self.assertEqual(sched["windows"][0]["seat_capacity"], 100)

    def test_shrink_auto_trim_drops_latest_and_notifies(self):
        """窗口收缩：按终审时间从新到旧移除，通知范围与失效通知明确。"""
        version = self._two_schools_locked()
        st, body, _ = self.api.post("/windows/win-1/shrink", {
            "new_seat_capacity": 30, "expected_version": version,
            "resolution": "auto_trim"}, CONTROL)
        self.assertEqual(st, 200, body)
        # plan-b 终审更晚，被移除
        self.assertEqual([d["plan_id"] for d in body["dropped"]], ["plan-b"])
        self.assertEqual(body["used_seats_after"], 30)
        self.assertIn("plan-b", body["explanation"])
        self.assertEqual(body["notification_scope"]["control"], True)
        self.assertEqual(set(body["notification_scope"]["schools"]), {"sch-a", "sch-b"})
        # plan-b 的占位通知已失效
        self.assertEqual(len(body["superseded_notifications"]), 1)
        # 排期反映移除结果
        st, sched, _ = self.api.get("/schedule", CONTROL)
        self.assertEqual(sched["windows"][0]["used_seats"], 30)
        self.assertEqual(sched["windows"][0]["seat_capacity"], 30)
        # 通知：sch-b 收到移除通知，sch-a 收到窗口变更通知
        st, notif, _ = self.api.get("/notifications?status=pending", CONTROL)
        by_school = {}
        for n in notif["notifications"]:
            by_school.setdefault(n["school_id"], []).append(n["kind"])
        self.assertIn("assignment_removed", by_school.get("sch-b", []))
        self.assertIn("window_changed", by_school.get("sch-a", []))

    def test_withdraw_frees_seats_and_supersedes(self):
        """学校退出：座位释放、原占位通知失效、总控收到通知。"""
        self._two_schools_locked()
        st, body, _ = self.api.post("/plans/plan-b/withdraw",
                                    {"reason": "校舍检修"}, school("sch-b"))
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "withdrawn")
        self.assertEqual(body["freed_seats"], 30)
        self.assertEqual(len(body["superseded_notifications"]), 1)
        self.assertTrue(body["notification_scope"]["control"])
        st, sched, _ = self.api.get("/schedule", CONTROL)
        self.assertEqual(sched["windows"][0]["used_seats"], 30)
        st, plan, _ = self.api.get("/plans/plan-b", CONTROL)
        self.assertEqual(plan["status"], "withdrawn")
        self.assertIsNone(plan["session_id"])
        # 总控收到退出通知
        st, notif, _ = self.api.get("/notifications?status=pending", CONTROL)
        control_notices = [n for n in notif["notifications"] if n["school_id"] is None]
        self.assertTrue(any(n["kind"] == "plan_withdrawn" for n in control_notices))

    def test_cancel_session_supersedes_pending_and_notifies(self):
        self._two_schools_locked()
        version = schedule_version(self.api)
        st, body, _ = self.api.post("/sessions/sess-1/cancel", {
            "reason": "空间站窗口调整", "expected_version": version}, CONTROL)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["freed_seats"], 60)
        self.assertEqual(len(body["superseded_notifications"]), 2)
        self.assertEqual(set(body["notification_scope"]["schools"]), {"sch-a", "sch-b"})
        st, sched, _ = self.api.get("/schedule", CONTROL)
        self.assertEqual(sched["windows"][0]["used_seats"], 0)
        self.assertEqual(sched["sessions"][0]["status"], "cancelled")

    def test_versions_restorable(self):
        """每次排期版本可还原：v1 是首次锁定，v2 是移除之后。"""
        self._two_schools_locked()
        st, body, _ = self.api.post("/plans/plan-b/withdraw", {}, school("sch-b"))
        self.assertEqual(st, 200, body)

        st, body, _ = self.api.get("/schedule/versions", CONTROL)
        self.assertEqual(st, 200)
        self.assertEqual(body["current_version"], 2)
        self.assertEqual([v["version"] for v in body["versions"]], [1, 2])
        self.assertTrue(any("退出" in (v["summary"] or "") for v in body["versions"]))

        st, v1, _ = self.api.get("/schedule/versions/1", CONTROL)
        self.assertEqual(st, 200)
        self.assertEqual(v1["windows"][0]["used_seats"], 60)
        self.assertEqual(len(v1["sessions"][0]["assignments"]), 2)

        st, v2, _ = self.api.get("/schedule/versions/2", CONTROL)
        self.assertEqual(v2["windows"][0]["used_seats"], 30)
        self.assertEqual(len(v2["sessions"][0]["assignments"]), 1)

        st, body, _ = self.api.get("/schedule/versions/99", CONTROL)
        self.assertEqual(st, 404)

    def test_shrink_requires_current_version(self):
        self._two_schools_locked()
        st, body, _ = self.api.post("/windows/win-1/shrink", {
            "new_seat_capacity": 50, "expected_version": 0}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "version_conflict")
        self.assertTrue(body["intervening"])


if __name__ == "__main__":
    unittest.main()
