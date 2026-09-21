"""断线重连与重启还原验收。"""
import tempfile
import unittest

from tests.helpers import CONTROL, Server, school, setup_plan


class ResumeTest(unittest.TestCase):
    def test_restart_recovers_state_and_versions(self):
        """重启后仍能还原每次排期版本，业务状态完整恢复。"""
        with tempfile.TemporaryDirectory() as data_dir:
            with Server(data_dir) as api:
                setup_plan(api, "sch-1", "plan-1", students=30)
                setup_plan(api, "sch-2", "plan-2", students=20, region="澳门")
                api.put("/plans/plan-1/students", {"students": [
                    {"student_id": "st-1", "name": "陈小明", "grade": "中四"}]},
                    school("sch-1"))
                st, body, _ = api.post("/sessions", {
                    "session_id": "sess-1", "window_id": "win-1",
                    "assignments": [{"plan_id": "plan-1", "seats": 30},
                                    {"plan_id": "plan-2", "seats": 20}],
                    "expected_version": 0}, CONTROL)
                self.assertEqual(st, 200, body)
                st, body, _ = api.post("/plans/plan-2/withdraw", {}, school("sch-2"))
                self.assertEqual(st, 200, body)
                st, before, _ = api.get("/schedule", CONTROL)
                st, v1_before, _ = api.get("/schedule/versions/1", CONTROL)
                st, events_before, _ = api.get("/events", CONTROL)
                latest_seq = events_before["latest_seq"]

            # 模拟重启：同一数据目录重新加载
            with Server(data_dir) as api:
                st, events_after, _ = api.get("/events", CONTROL)
                self.assertEqual(events_after["latest_seq"], latest_seq)
                st, after, _ = api.get("/schedule", CONTROL)
                self.assertEqual(after, before)
                self.assertEqual(after["version"], 2)
                self.assertEqual(after["windows"][0]["used_seats"], 30)
                # 每次排期版本仍可还原
                st, v1_after, _ = api.get("/schedule/versions/1", CONTROL)
                self.assertEqual(v1_after, v1_before)
                self.assertEqual(v1_after["windows"][0]["used_seats"], 50)
                # 学生名单、审核链、通知状态完整
                st, students, _ = api.get("/plans/plan-1/students?purpose=重启后核对",
                                          CONTROL)
                self.assertEqual(len(students["students"]), 1)
                st, plan, _ = api.get("/plans/plan-2", CONTROL)
                self.assertEqual(plan["status"], "withdrawn")
                self.assertEqual(len(plan["review_chain"]), 2)
                st, notif, _ = api.get("/notifications?status=superseded", CONTROL)
                self.assertEqual(len(notif["notifications"]), 1)

    def test_events_since_last_confirmed(self):
        """断线重连后能从最后确认点继续：事件连续、无遗漏、无重复。"""
        with Server() as api:
            setup_plan(api, "sch-1", "plan-1", students=30)
            st, checkpoint, _ = api.get("/events", CONTROL)
            confirmed = checkpoint["latest_seq"]
            # 断线期间发生的变更
            api.post("/sessions", {
                "session_id": "sess-1", "window_id": "win-1",
                "assignments": [{"plan_id": "plan-1", "seats": 30}],
                "expected_version": 0}, CONTROL)
            api.post("/sessions/sess-1/cancel",
                     {"reason": "窗口调整", "expected_version": 1}, CONTROL)
            # 重连：从最后确认点续传
            st, body, _ = api.get(f"/events?since={confirmed}", CONTROL)
            self.assertEqual(st, 200)
            events = body["events"]
            self.assertTrue(events)
            seqs = [e["seq"] for e in events]
            self.assertEqual(seqs, list(range(confirmed + 1, body["latest_seq"] + 1)))
            types = [e["type"] for e in events]
            self.assertIn("session_locked", types)
            self.assertIn("session_cancelled", types)

    def test_idempotency_survives_restart(self):
        """重启后重放同一幂等键：返回原结果，不重复落事件。"""
        with tempfile.TemporaryDirectory() as data_dir:
            with Server(data_dir) as api:
                setup_plan(api, "sch-1", "plan-1", students=30)
                st1, body1, _ = api.post("/sessions", {
                    "session_id": "sess-1", "window_id": "win-1",
                    "assignments": [{"plan_id": "plan-1", "seats": 30}]},
                    CONTROL, idem="lock-req-1")
                self.assertEqual(st1, 200, body1)
                st, events, _ = api.get("/events", CONTROL)
                seq_before = events["latest_seq"]

            with Server(data_dir) as api:
                st2, body2, h2 = api.post("/sessions", {
                    "session_id": "sess-1", "window_id": "win-1",
                    "assignments": [{"plan_id": "plan-1", "seats": 30}]},
                    CONTROL, idem="lock-req-1")
                self.assertEqual(st2, 200)
                self.assertEqual(body2, body1)
                self.assertEqual(h2.get("idempotent-replay"), "true")
                st, events, _ = api.get("/events", CONTROL)
                self.assertEqual(events["latest_seq"], seq_before)
                st, sched, _ = api.get("/schedule", CONTROL)
                self.assertEqual(sched["version"], 1)


if __name__ == "__main__":
    unittest.main()
