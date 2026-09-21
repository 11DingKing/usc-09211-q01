"""参与计划、审核链与幂等上报验收。"""
import unittest

from service.models import ApiError, PlanStatus, Student
from service.service import Actor
from tests.helpers import CONTROL, make_service, school_actor, seed_world, submit_approved_plan


class PlanLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.app = make_service()
        seed_world(self.app)

    def test_full_approval_chain(self):
        submit_approved_plan(self.app, "school-A", "plan-A1", 3)
        plan = self.app.plans["plan-A1"]
        self.assertEqual(plan.status, PlanStatus.CONTROL_APPROVED)
        self.assertEqual([a["stage"] for a in plan.approvals], ["school", "control"])
        self.assertEqual(plan.questions[0].text, "能看到地球吗？")
        self.assertIn("课后材料-plan-A1.pdf", plan.materials)

    def test_control_cannot_skip_school_review(self):
        actor = school_actor("school-A")
        student_ids = [f"stu-skip-0"]
        self.app.add_student(actor, Student(
            id="stu-skip-0", school_id="school-A", name="甲",
            grade="初一", contact="a@x.test"))
        self.app.submit_plan(
            actor, "plan-skip", "w1", 1, student_ids, [], [],
            idempotency_key="k-skip")
        with self.assertRaises(ApiError) as cm:
            self.app.approve_or_reject(CONTROL, "plan-skip", "approve", "越级")
        self.assertEqual(cm.exception.code, "APPROVAL_STAGE")

    def test_duplicate_submission_is_idempotent(self):
        actor = school_actor("school-A")
        self.app.add_student(actor, Student(
            id="stu-dup-0", school_id="school-A", name="甲", grade="初一",
            contact="a@x.test"))
        kwargs = dict(window_id="w1", seats=1, student_ids=["stu-dup-0"],
                      questions=[], materials=[], idempotency_key="same-key")
        first = self.app.submit_plan(actor, "plan-dup", **kwargs)
        # 重复上报（即使使用不同 plan_id）必须命中同一幂等键，不产生第二条计划。
        second = self.app.submit_plan(actor, "plan-dup-again", **kwargs)
        self.assertIs(first, second)
        submitted = [e for e in self.app.store.all_events if e["type"] == "PLAN_SUBMITTED"]
        self.assertEqual(len(submitted), 1)

    def test_duplicate_approval_is_idempotent(self):
        submit_approved_plan(self.app, "school-A", "plan-A3", 2)
        before = len(self.app.store.all_events)
        self.app.approve_or_reject(CONTROL, "plan-A3", "approve", "重复点击")
        self.assertEqual(len(self.app.store.all_events), before)

    def test_school_cannot_review_other_school_plan(self):
        submit_approved_plan(self.app, "school-B", "plan-B1", 2)
        with self.assertRaises(ApiError) as cm:
            self.app.approve_or_reject(school_actor("school-A"), "plan-B1",
                                       "reject", "越校驳回")
        self.assertEqual(cm.exception.status, 403)

    def test_withdraw_before_lock_releases_hold(self):
        submit_approved_plan(self.app, "school-A", "plan-A5", 2)
        self.app.hold_plan(CONTROL, "plan-A5")
        self.assertEqual(self.app._used_seats("w1"), 2)
        self.app.withdraw_plan(school_actor("school-A"), "plan-A5", "临时退出")
        self.assertEqual(self.app._used_seats("w1"), 0)
        self.assertEqual(self.app.plans["plan-A5"].status, PlanStatus.WITHDRAWN)

    def test_withdraw_is_idempotent(self):
        submit_approved_plan(self.app, "school-A", "plan-A6", 1)
        self.app.withdraw_plan(school_actor("school-A"), "plan-A6", "退出")
        before = len(self.app.store.all_events)
        self.app.withdraw_plan(school_actor("school-A"), "plan-A6", "再次退出")
        self.assertEqual(len(self.app.store.all_events), before)

    def test_auditor_is_read_only(self):
        auditor = Actor(id="aud-1", role="auditor")
        with self.assertRaises(ApiError) as cm:
            self.app.submit_plan(
                auditor, "plan-x", "w1", 1, [], [], [], idempotency_key="x")
        self.assertEqual(cm.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
