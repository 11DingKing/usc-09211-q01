"""重启还原排期版本与断线重连验收。"""
import os
import tempfile
import unittest

from service.eventstore import EventStore
from service.service import ApplicationService
from tests.helpers import CONTROL, school_actor, seed_world, submit_approved_plan


class PersistenceTest(unittest.TestCase):
    def test_restart_restores_every_schedule_version(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(path)
        try:
            app = ApplicationService(EventStore(path))
            seed_world(app, capacity=10)
            submit_approved_plan(app, "school-A", "plan-1", 3)
            submit_approved_plan(app, "school-B", "plan-2", 3)
            for pid in ("plan-1", "plan-2"):
                app.hold_plan(CONTROL, pid)
            app.lock_window(CONTROL, "w1", "v1 锁定")
            app.withdraw_plan(school_actor("school-B"), "plan-2", "退出",
                              expected_version=1)
            app.dispatch_notices(CONTROL)
            v1_before = app.version_view(1)
            v2_before = app.version_view(2)
            schedule_before = app.schedule_view()

            # 重启：新实例仅从事件日志重放。
            restored = ApplicationService(EventStore(path))
            self.assertEqual(restored.schedule_view(), schedule_before)
            self.assertEqual(restored.version_view(1), v1_before)
            self.assertEqual(restored.version_view(2), v2_before)
            self.assertEqual(restored.current_version, 2)
            # 计划、占位、通知、审计全部还原。
            self.assertEqual(restored.plans["plan-2"].status.value, "WITHDRAWN")
            self.assertEqual(restored.holds["hold-plan-2"].status.value, "RELEASED")
            self.assertTrue(any(
                n.kind == "PLAN_WITHDRAWN" for n in restored.notices.values()))
            self.assertTrue(any(
                a["action"] == "withdraw_plan" and a["decision"] == "ALLOWED"
                for a in restored.audit))
            self.assertTrue(os.path.exists(path))
        finally:
            os.remove(path)

    def test_event_log_is_append_only_on_disk(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(path)
        try:
            app = ApplicationService(EventStore(path))
            seed_world(app)
            with open(path, encoding="utf-8") as fh:
                first_lines = len(fh.readlines())
            submit_approved_plan(app, "school-A", "plan-1", 1)
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertGreater(len(lines), first_lines)
            # 早期事件内容保持不变（未被覆盖改写）。
            self.assertIn("SCHOOL_REGISTERED", lines[0])
        finally:
            os.remove(path)


class SyncResumeTest(unittest.TestCase):
    def setUp(self):
        self.app = ApplicationService(EventStore())
        seed_world(self.app, capacity=10)
        submit_approved_plan(self.app, "school-A", "plan-1", 2)
        submit_approved_plan(self.app, "school-B", "plan-2", 2)
        for pid in ("plan-1", "plan-2"):
            self.app.hold_plan(CONTROL, pid)
        self.app.lock_window(CONTROL, "w1", "锁定")

    def test_resume_from_last_confirmed_cursor(self):
        cursor = self.app.store.all_events[-1]["seq"]
        self.app.withdraw_plan(school_actor("school-A"), "plan-1", "退出",
                               expected_version=1)
        batch = self.app.sync(school_actor("school-A"), cursor)
        kinds = [e["type"] for e in batch["events"]]
        self.assertIn("HOLD_RELEASED", kinds)
        self.assertIn("SCHEDULE_VERSION_BUMPED", kinds)
        self.assertEqual(batch["schedule_version"], 2)
        # 用新游标再次拉取为空：确认点语义正确，不重复不遗漏。
        again = self.app.sync(school_actor("school-A"), batch["cursor"])
        self.assertEqual(again["events"], [])
        self.assertEqual(again["cursor"], batch["cursor"])

    def test_sync_is_role_scoped(self):
        cursor = self.app.store.all_events[-1]["seq"]
        self.app.withdraw_plan(school_actor("school-B"), "plan-2", "退出",
                               expected_version=1)
        batch_a = self.app.sync(school_actor("school-A"), 0)
        # A 校重连时看不到 B 校的计划与学生事件。
        for event in batch_a["events"]:
            p = event["payload"]
            if event["type"] in ("PLAN_SUBMITTED", "HOLD_HELD"):
                self.assertEqual(p.get("school_id"), "school-A")
        # 但窗口与排期版本等全局信息仍可见。
        self.assertIn("WINDOW_ADDED", [e["type"] for e in batch_a["events"]])
        # B 校退出事件对 A 校不可见（HOLD_HELD 有 school_id，PLAN_STATUS_CHANGED 按计划过滤）。
        b_status = [e for e in batch_a["events"]
                    if e["type"] == "PLAN_STATUS_CHANGED"
                    and e["payload"]["plan_id"] == "plan-2"]
        self.assertEqual(b_status, [])
        # 全量游标一致性：两校拿到的 cursor 相同。
        batch_b = self.app.sync(school_actor("school-B"), cursor)
        self.assertEqual(batch_b["cursor"], batch_a["cursor"])


if __name__ == "__main__":
    unittest.main()
