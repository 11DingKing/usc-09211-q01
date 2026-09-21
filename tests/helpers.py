"""验收测试公用构造。"""
from service.eventstore import EventStore
from service.models import Student
from service.service import Actor, ApplicationService


CONTROL = Actor(id="ctrl-1", role="control")


def make_service(path=None) -> ApplicationService:
    return ApplicationService(EventStore(path))


def school_actor(school_id: str) -> Actor:
    return Actor(id=f"user-{school_id}", role="school", school_id=school_id)


def seed_world(app: ApplicationService, *, window_id="w1", capacity=10,
               signal=None, schools=("A", "B")):
    """注册两地学校、校区、窗口。返回窗口与容量信息。"""
    for sid in schools:
        region = "HK" if sid == "A" else "MO"
        app.register_school(CONTROL, f"school-{sid}", f"港澳学校{sid}", region)
        app.register_campus(CONTROL, f"campus-{sid}", f"school-{sid}", capacity)
    window = app.add_window(
        CONTROL, window_id, "campus-A", "2026-10-01T09:00:00Z",
        "2026-10-01T09:20:00Z", signal or capacity)
    return window


def add_students(app, school_id, n, prefix=None):
    actor = school_actor(school_id)
    prefix = prefix or f"{school_id}-stu"
    ids = []
    for i in range(n):
        sid = f"{prefix}-{i}"
        app.add_student(actor, Student(
            id=sid, school_id=school_id, name=f"学生{school_id}{i}",
            grade="初一", contact=f"{school_id}-{i}@contact.test"))
        ids.append(sid)
    return ids


def submit_approved_plan(app, school_id, plan_id, seats, *, window_id="w1"):
    """学校上报 → 校内审核 → 总控审核通过。"""
    actor = school_actor(school_id)
    student_ids = add_students(app, school_id, seats, prefix=f"stu-{plan_id}")
    app.submit_plan(
        actor, plan_id, window_id, seats, student_ids,
        questions=[{"id": f"q-{plan_id}-0", "text": "能看到地球吗？",
                    "student_id": student_ids[0]}],
        materials=[f"课后材料-{plan_id}.pdf"],
        idempotency_key=f"idem-{plan_id}")
    app.approve_or_reject(actor, plan_id, "approve", "校内同意")
    app.approve_or_reject(CONTROL, plan_id, "approve", "总控同意")
    return plan_id
