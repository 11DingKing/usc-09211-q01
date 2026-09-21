"""参与计划与审核链测试。"""
import unittest

from tests.helpers import CONTROL, REVIEWER, Server, approve_plan, school, setup_plan


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.api = self.server.__enter__()

    def tearDown(self):
        self.server.__exit__()

    def _register(self, school_id="sch-1", cap=50):
        st, body, _ = self.api.post("/schools", {
            "school_id": school_id, "name": "培正中学", "region": "香港",
            "seat_capacity": cap}, CONTROL)
        self.assertEqual(st, 200, body)
        st, body, _ = self.api.post("/windows", {
            "window_id": "win-1", "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T11:00:00+08:00", "seat_capacity": 100}, CONTROL)
        self.assertEqual(st, 200, body)

    def test_submit_and_full_review_chain(self):
        self._register()
        st, body, _ = self.api.post("/plans", {
            "plan_id": "plan-1", "school_id": "sch-1", "window_id": "win-1",
            "student_count": 30}, school("sch-1"))
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "submitted")

        st, body, _ = self.api.post("/plans/plan-1/reviews", {
            "stage": "initial", "action": "approve", "comment": "材料齐全"}, REVIEWER)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "initial_approved")

        st, body, _ = self.api.post("/plans/plan-1/reviews", {
            "stage": "final", "action": "approve", "comment": "同意"}, CONTROL)
        self.assertEqual(st, 200, body)
        self.assertEqual(body["status"], "approved")

        st, body, _ = self.api.get("/plans/plan-1", CONTROL)
        self.assertEqual(st, 200)
        self.assertEqual([e["stage"] for e in body["review_chain"]], ["initial", "final"])
        self.assertEqual(body["review_chain"][0]["actor"], "rev-1")

    def test_idempotent_replay_same_key(self):
        """重复上报保持幂等：同一 Idempotency-Key 重放返回原结果，不产生重复记录。"""
        self._register()
        payload = {"plan_id": "plan-1", "school_id": "sch-1",
                   "window_id": "win-1", "student_count": 30}
        st1, body1, h1 = self.api.post("/plans", payload, school("sch-1"), idem="req-001")
        st2, body2, h2 = self.api.post("/plans", payload, school("sch-1"), idem="req-001")
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(body1, body2)
        self.assertNotIn("idempotent-replay", h1)
        self.assertEqual(h2.get("idempotent-replay"), "true")
        st, body, _ = self.api.get("/plans", CONTROL)
        self.assertEqual(len(body["plans"]), 1)

    def test_same_plan_id_different_payload_conflicts(self):
        self._register()
        self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                 "window_id": "win-1", "student_count": 30}, school("sch-1"))
        st, body, _ = self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                               "window_id": "win-1", "student_count": 40},
                                    school("sch-1"))
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "already_exists")

    def test_duplicate_active_plan_same_window_conflicts(self):
        self._register()
        self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                 "window_id": "win-1", "student_count": 30}, school("sch-1"))
        st, body, _ = self.api.post("/plans", {"plan_id": "plan-2", "school_id": "sch-1",
                                               "window_id": "win-1", "student_count": 20},
                                    school("sch-1"))
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "duplicate_plan")
        self.assertEqual(body["existing_plan_id"], "plan-1")

    def test_final_before_initial_rejected(self):
        self._register()
        self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                 "window_id": "win-1", "student_count": 30}, school("sch-1"))
        st, body, _ = self.api.post("/plans/plan-1/reviews", {
            "stage": "final", "action": "approve"}, CONTROL)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "invalid_state")

    def test_reviewer_cannot_final_review(self):
        self._register()
        self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                 "window_id": "win-1", "student_count": 30}, school("sch-1"))
        self.api.post("/plans/plan-1/reviews", {"stage": "initial", "action": "approve"}, REVIEWER)
        st, body, _ = self.api.post("/plans/plan-1/reviews", {
            "stage": "final", "action": "approve"}, REVIEWER)
        self.assertEqual(st, 403)

    def test_reject_flow(self):
        self._register()
        self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                 "window_id": "win-1", "student_count": 30}, school("sch-1"))
        st, body, _ = self.api.post("/plans/plan-1/reviews", {
            "stage": "initial", "action": "reject", "comment": "人数与名单不符"}, REVIEWER)
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "rejected")
        st, body, _ = self.api.post("/plans/plan-1/reviews", {
            "stage": "initial", "action": "approve"}, REVIEWER)
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "invalid_state")

    def test_school_cannot_submit_for_other_school(self):
        self._register("sch-1")
        st, body, _ = self.api.post("/schools", {
            "school_id": "sch-2", "name": "澳门培正", "region": "澳门",
            "seat_capacity": 40}, CONTROL)
        self.assertEqual(st, 200, body)
        st, body, _ = self.api.post("/plans", {"plan_id": "plan-9", "school_id": "sch-2",
                                               "window_id": "win-1", "student_count": 10},
                                    school("sch-1"))
        self.assertEqual(st, 403)

    def test_student_count_within_school_capacity(self):
        self._register(cap=20)
        st, body, _ = self.api.post("/plans", {"plan_id": "plan-1", "school_id": "sch-1",
                                               "window_id": "win-1", "student_count": 30},
                                    school("sch-1"))
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "capacity_exceeded")
        self.assertEqual(body["school_seat_capacity"], 20)

    def test_school_role_requires_school_header(self):
        st, body, _ = self.api.call("POST", "/plans",
                                    body={"plan_id": "p", "school_id": "sch-1",
                                          "window_id": "win-1", "student_count": 1},
                                    headers={"X-Actor-Id": "t", "X-Actor-Role": "school"})
        self.assertEqual(st, 401)


if __name__ == "__main__":
    unittest.main()
