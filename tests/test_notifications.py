"""通知验收：失效通知不会误发，下发范围正确。"""
import unittest

from tests.helpers import CONTROL, Server, school, setup_plan


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.api = self.server.__enter__()

    def tearDown(self):
        self.server.__exit__()

    def _lock_two_schools(self):
        setup_plan(self.api, "sch-a", "plan-a", students=30)
        setup_plan(self.api, "sch-b", "plan-b", students=30, region="澳门")
        st, body, _ = self.api.post("/sessions", {
            "session_id": "sess-1", "window_id": "win-1",
            "assignments": [{"plan_id": "plan-a", "seats": 30},
                            {"plan_id": "plan-b", "seats": 30}],
            "expected_version": 0}, CONTROL)
        self.assertEqual(st, 200, body)

    def _notifications(self, status=None):
        path = "/notifications" + (f"?status={status}" if status else "")
        st, body, _ = self.api.get(path, CONTROL)
        self.assertEqual(st, 200)
        return body["notifications"]

    def test_dispatch_sends_pending_only(self):
        self._lock_two_schools()
        pending = self._notifications("pending")
        # 两校锁定通知 + 各校两条审核结果通知（初审、终审）
        lock_notices = [n for n in pending if n["kind"] == "session_locked"]
        self.assertEqual(len(lock_notices), 2)

        st, body, _ = self.api.post("/notifications/dispatch", {}, CONTROL)
        self.assertEqual(st, 200)
        self.assertEqual(body["dispatched_count"], len(pending))
        self.assertEqual(set(body["dispatched"]), {n["notification_id"] for n in pending})
        # 再次下发为空（天然幂等）
        st, body, _ = self.api.post("/notifications/dispatch", {}, CONTROL)
        self.assertEqual(body["dispatched_count"], 0)
        self.assertEqual(self._notifications("pending"), [])

    def test_superseded_never_dispatched(self):
        """失效通知不会误发：占位通知被撤回取代后，下发只含有效通知。"""
        self._lock_two_schools()
        stale = [n for n in self._notifications("pending")
                 if n["kind"] == "session_locked" and n["school_id"] == "sch-b"]
        self.assertEqual(len(stale), 1)
        stale_id = stale[0]["notification_id"]

        st, body, _ = self.api.post("/plans/plan-b/withdraw", {}, school("sch-b"))
        self.assertEqual(st, 200)
        self.assertIn(stale_id, body["superseded_notifications"])

        st, body, _ = self.api.post("/notifications/dispatch", {}, CONTROL)
        self.assertNotIn(stale_id, body["dispatched"])
        self.assertIn(stale_id, body["skipped_superseded"])
        # sch-b 不会收到任何“已锁定场次”的消息
        st, all_notif, _ = self.api.get("/notifications", CONTROL)
        sent_to_b = [n for n in all_notif["notifications"]
                     if n["school_id"] == "sch-b" and n["status"] == "sent"]
        self.assertTrue(all(n["kind"] != "session_locked" for n in sent_to_b))
        stale_view = [n for n in all_notif["notifications"]
                      if n["notification_id"] == stale_id][0]
        self.assertEqual(stale_view["status"], "superseded")
        self.assertIsNotNone(stale_view["superseded_reason"])

    def test_school_sees_only_own_notifications(self):
        self._lock_two_schools()
        st, body, _ = self.api.get("/notifications", school("sch-a"))
        self.assertEqual(st, 200)
        self.assertTrue(body["notifications"])
        self.assertTrue(all(n["school_id"] == "sch-a" for n in body["notifications"]))
        # 学校角色显式请求他校通知被拒绝
        st, body, _ = self.api.get("/notifications?school_id=sch-b", school("sch-a"))
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
