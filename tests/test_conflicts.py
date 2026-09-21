"""窗口缩短、两地同时改动的冲突处置与通知范围验收。"""
import unittest

from service.models import (
    ApiError, ConflictKind, ConflictStatus, NoticeStatus, PlanStatus)
from tests.helpers import CONTROL, make_service, school_actor, seed_world, submit_approved_plan


class ConflictAndNoticeTest(unittest.TestCase):
    def setUp(self):
        self.app = make_service()
        seed_world(self.app, capacity=10, signal=10)

    def _locked_three(self):
        # 三个计划各 3 席，共 9 席锁定在 w1。
        submit_approved_plan(self.app, "school-A", "plan-1", 3)
        submit_approved_plan(self.app, "school-B", "plan-2", 3)
        submit_approved_plan(self.app, "school-A", "plan-3", 3)
        for pid in ("plan-1", "plan-2", "plan-3"):
            self.app.hold_plan(CONTROL, pid)
        self.app.lock_window(CONTROL, "w1", "首期锁定")

    def test_window_shortened_opens_explained_conflict_with_scope(self):
        self._locked_three()
        # 窗口容量缩到 6 → 超出 3 席；必须挂起冲突，不能静默改排期。
        with self.assertRaises(ApiError) as cm:
            self.app.revise_window(
                CONTROL, "w1", "2026-10-01T09:05:00Z", "2026-10-01T09:15:00Z",
                6, "空间站过境窗口缩短", expected_version=1)
        self.assertEqual(cm.exception.code, "CONFLICT_OPEN")
        conflict_id = cm.exception.extra["conflict_id"]
        conflict = self.app.conflicts[conflict_id]
        self.assertEqual(conflict.kind, ConflictKind.WINDOW_SHORTENED)
        self.assertEqual(conflict.status, ConflictStatus.OPEN)
        self.assertEqual(conflict.detail["overflow_seats"], 3)
        self.assertEqual(conflict.suggested, "DROP_LATEST")
        # 通知范围明确：涉及窗口内全部三个计划、两地学校。
        scope = conflict.detail["notification_scope"]
        self.assertEqual(sorted(scope["plan_ids"]), ["plan-1", "plan-2", "plan-3"])
        self.assertEqual(scope["schools"], ["school-A", "school-B"])

    def test_notices_wait_until_conflict_resolved(self):
        self._locked_three()
        with self.assertRaises(ApiError):
            self.app.revise_window(
                CONTROL, "w1", "2026-10-01T09:05:00Z", "2026-10-01T09:15:00Z",
                6, "缩短", expected_version=1)
        # 冲突未处置：派发时一条都不能误发，只挂起。
        out = self.app.dispatch_notices(CONTROL)
        self.assertEqual(out["dispatched"], [])
        statuses = {n.id: n.status for n in self.app.notices.values()}
        self.assertTrue(all(s == NoticeStatus.PENDING for s in statuses.values()))

    def test_resolve_drop_latest_dispatches_correct_scope(self):
        self._locked_three()
        with self.assertRaises(ApiError) as cm:
            self.app.revise_window(
                CONTROL, "w1", "2026-10-01T09:05:00Z", "2026-10-01T09:15:00Z",
                6, "缩短", expected_version=1)
        conflict_id = cm.exception.extra["conflict_id"]
        self.app.resolve_conflict(CONTROL, conflict_id, "DROP_LATEST")
        out = self.app.dispatch_notices(CONTROL)
        by_plan = {}
        for item in out["dispatched"]:
            notice = self.app.notices[item["id"]]
            if notice.audience == "participants":
                by_plan.setdefault(notice.scope_plan_ids[0], notice.kind)
        # plan-3（编号靠后）收到移出通知，plan-1/plan-2 收到场次变更通知。
        self.assertEqual(by_plan["plan-3"], "PLAN_REMOVED")
        self.assertEqual(by_plan["plan-1"], "SESSION_CHANGED")
        self.assertEqual(by_plan["plan-2"], "SESSION_CHANGED")
        # 新版本容量自洽：保留 6 席。
        self.assertEqual(self.app._used_seats("w1"), 6)
        self.assertEqual(self.app.plans["plan-3"].status, PlanStatus.CONTROL_APPROVED)

    def test_cancel_session_notifies_everyone_and_drains_session(self):
        self._locked_three()
        with self.assertRaises(ApiError) as cm:
            self.app.revise_window(
                CONTROL, "w1", "2026-10-01T09:05:00Z", "2026-10-01T09:15:00Z",
                4, "大幅缩短", expected_version=1)
        conflict_id = cm.exception.extra["conflict_id"]
        self.app.resolve_conflict(CONTROL, conflict_id, "CANCEL_SESSION")
        self.app.dispatch_notices(CONTROL)
        kinds = {n.scope_plan_ids[0]: n.kind for n in self.app.notices.values()
                 if n.audience == "participants" and n.status == NoticeStatus.DISPATCHED}
        self.assertEqual(set(kinds.values()), {"SESSION_CANCELLED"})
        self.assertEqual(set(kinds), {"plan-1", "plan-2", "plan-3"})
        self.assertNotIn("w1", self.app.current_sessions)

    def test_simultaneous_edit_detected_on_stale_version(self):
        self._locked_three()
        # 总控先把版本推进到 v2（窗口无冲突调整）。
        self.app.revise_window(
            CONTROL, "w1", "2026-10-01T09:01:00Z", "2026-10-01T09:19:00Z",
            10, "微调时间", expected_version=1)
        # 学校仍拿着 v1 退出 → 必须报可解释的并发冲突。
        with self.assertRaises(ApiError) as cm:
            self.app.withdraw_plan(
                school_actor("school-A"), "plan-3", "我要退出", expected_version=1)
        self.assertEqual(cm.exception.code, "SIMULTANEOUS_EDIT")
        self.assertEqual(cm.exception.extra["current_version"], 2)
        conflict = next(c for c in self.app.conflicts.values()
                        if c.kind == ConflictKind.SIMULTANEOUS_EDIT)
        self.assertIn("v2", conflict.detected_reasons[1])
        # 带着新版本重试即成功。
        self.app.withdraw_plan(
            school_actor("school-A"), "plan-3", "我要退出", expected_version=2)
        self.assertEqual(self.app.plans["plan-3"].status, PlanStatus.WITHDRAWN)

    def test_suppressed_notice_is_never_sent(self):
        self._locked_three()
        # 学校退出 plan-3，新版本生成；旧的 SESSION_CONFIRMED 草稿必须被抑制。
        self.app.withdraw_plan(
            school_actor("school-A"), "plan-3", "退出", expected_version=1)
        out = self.app.dispatch_notices(CONTROL)
        suppressed = {s["id"]: s["reason"] for s in out["suppressed"]}
        old_confirm = next(n.id for n in self.app.notices.values()
                           if n.kind == "SESSION_CONFIRMED"
                           and n.scope_plan_ids == ["plan-3"])
        self.assertIn(old_confirm, suppressed)
        self.assertEqual(suppressed[old_confirm], "SUPERSEDED_BY_NEWER_NOTICE")
        # 抑制的通知没有任何收件人，且永不转为已派发。
        self.assertEqual(self.app.notices[old_confirm].status, NoticeStatus.SUPPRESSED)
        self.assertIsNone(self.app.notices[old_confirm].recipients)
        # plan-3 只收到退出相关通知，确认通知未误发给学生。
        sent_kinds = [n.kind for n in self.app.notices.values()
                      if n.status == NoticeStatus.DISPATCHED
                      and n.scope_plan_ids == ["plan-3"]]
        self.assertEqual(sent_kinds, ["PLAN_WITHDRAWN"])

    def test_dispatch_is_idempotent(self):
        self._locked_three()
        self.app.withdraw_plan(
            school_actor("school-A"), "plan-3", "退出", expected_version=1)
        first = self.app.dispatch_notices(CONTROL)
        second = self.app.dispatch_notices(CONTROL)
        self.assertGreater(len(first["dispatched"]), 0)
        self.assertEqual(second["dispatched"], [])
        self.assertEqual(second["suppressed"], [])


if __name__ == "__main__":
    unittest.main()
