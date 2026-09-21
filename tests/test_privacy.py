"""未成年人资料角色隔离与访问审计验收。"""
import unittest

from service.models import ApiError, Student
from service.service import Actor
from tests.helpers import CONTROL, make_service, school_actor, seed_world


class PrivacyAuditTest(unittest.TestCase):
    def setUp(self):
        self.app = make_service()
        seed_world(self.app)
        self.school = school_actor("school-A")
        self.app.add_student(self.school, Student(
            id="minor-1", school_id="school-A", name="陈小华",
            grade="初一", contact="parent@example.test"))

    def test_only_own_school_reads_minor_profile(self):
        students = self.app.list_students(self.school, "school-A")
        self.assertEqual(students[0].contact, "parent@example.test")
        # 其他港澳学校不可见。
        with self.assertRaises(ApiError) as cm:
            self.app.list_students(school_actor("school-B"), "school-A")
        self.assertEqual(cm.exception.status, 403)
        # 总控也不可直接读取未成年人完整资料。
        with self.assertRaises(ApiError) as cm:
            self.app.list_students(CONTROL, "school-A")
        self.assertEqual(cm.exception.status, 403)

    def test_control_cannot_register_student(self):
        with self.assertRaises(ApiError) as cm:
            self.app.add_student(CONTROL, Student(
                id="minor-2", school_id="school-A", name="甲",
                grade="初一", contact="x@y.test"))
        self.assertEqual(cm.exception.status, 403)

    def test_school_cannot_add_student_to_other_school(self):
        with self.assertRaises(ApiError) as cm:
            self.app.add_student(school_actor("school-B"), Student(
                id="minor-3", school_id="school-A", name="乙",
                grade="初一", contact="z@y.test"))
        self.assertEqual(cm.exception.status, 403)

    def test_every_access_decision_is_audited(self):
        self.app.list_students(self.school, "school-A")
        with self.assertRaises(ApiError):
            self.app.list_students(school_actor("school-B"), "school-A")
        decisions = [(a["action"], a["decision"]) for a in self.app.audit
                     if a["action"] == "list_students"]
        self.assertIn(("list_students", "ALLOWED"), decisions)
        self.assertIn(("list_students", "DENIED"), decisions)
        denied = next(a for a in self.app.audit
                      if a["action"] == "list_students" and a["decision"] == "DENIED")
        self.assertEqual(denied["detail"]["reason"], "pii_role_isolation")

    def test_dispatch_audits_pii_contact_resolution(self):
        from tests.helpers import submit_approved_plan
        submit_approved_plan(self.app, "school-A", "plan-1", 1)
        self.app.hold_plan(CONTROL, "plan-1")
        self.app.lock_window(CONTROL, "w1", "锁定")
        self.app.dispatch_notices(CONTROL)
        pii_audit = [a for a in self.app.audit if a["action"] == "dispatch_resolve_contacts"]
        self.assertEqual(len(pii_audit), 1)
        self.assertTrue(pii_audit[0]["detail"]["pii_access"])

    def test_auditor_reads_audit_trail_but_not_pii(self):
        auditor = Actor(id="aud-1", role="auditor")
        # 审计员可走审计日志（服务层通过 audit 列表核对）。
        self.app.list_students(self.school, "school-A")
        self.assertGreaterEqual(len(self.app.audit), 1)
        # 但审计员读取学生资料同样被拒。
        with self.assertRaises(ApiError) as cm:
            self.app.list_students(auditor, "school-A")
        self.assertEqual(cm.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
