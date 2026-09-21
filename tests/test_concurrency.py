"""并发验收：并发占位不会超额，并发重复上报只落一条。

在领域层用屏障制造最大竞争，直接验证核心不变量。
"""
import threading
import unittest

from service.auth import Actor
from service.domain import Domain, DomainError
from service.store import EventStore

CONTROL = Actor("ops-1", "control")
REVIEWER = Actor("rev-1", "reviewer")


def _make_domain(schools=12, window_cap=100, students=30):
    domain = Domain(EventStore())
    domain.register_window(CONTROL, {
        "window_id": "win-1", "start": "2026-10-01T10:00:00+08:00",
        "end": "2026-10-01T11:00:00+08:00", "seat_capacity": window_cap})
    for i in range(schools):
        sid = f"sch-{i}"
        domain.register_school(CONTROL, {
            "school_id": sid, "name": f"学校{i}", "region": "香港", "seat_capacity": 40})
        domain.submit_plan(Actor(f"t-{i}", "school", sid), {
            "plan_id": f"plan-{i}", "school_id": sid, "window_id": "win-1",
            "student_count": students})
        domain.review_plan(REVIEWER, f"plan-{i}", {"stage": "initial", "action": "approve"})
        domain.review_plan(CONTROL, f"plan-{i}", {"stage": "final", "action": "approve"})
    return domain


class ConcurrencyAcceptanceTest(unittest.TestCase):
    def test_concurrent_locks_never_overbook(self):
        """12 个线程各锁 30 座，窗口容量 100：恰好 3 个成功，总占位不超容。"""
        domain = _make_domain(schools=12, window_cap=100, students=30)
        barrier = threading.Barrier(12)
        outcomes, outcomes_lock = [], threading.Lock()

        def worker(i):
            barrier.wait(timeout=10)
            try:
                domain.lock_session(CONTROL, {
                    "session_id": f"sess-{i}", "window_id": "win-1",
                    "assignments": [{"plan_id": f"plan-{i}", "seats": 30}]})
                with outcomes_lock:
                    outcomes.append("ok")
            except DomainError as exc:
                with outcomes_lock:
                    outcomes.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(outcomes.count("ok"), 3)
        self.assertEqual(outcomes.count("capacity_exceeded"), 9)
        # 不变量：总占位不超容量，且与投影一致
        self.assertEqual(domain._window_usage("win-1"), 90)
        self.assertLessEqual(domain._window_usage("win-1"), 100)
        locked = [s for s in domain.sessions.values() if s["status"] == "locked"]
        self.assertEqual(len(locked), 3)

    def test_concurrent_duplicate_submission_single_record(self):
        """8 个线程用同一幂等键重复上报：只落一条事件，结果一致。"""
        domain = Domain(EventStore())
        domain.register_window(CONTROL, {
            "window_id": "win-1", "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T11:00:00+08:00", "seat_capacity": 100})
        domain.register_school(CONTROL, {
            "school_id": "sch-1", "name": "培正", "region": "香港", "seat_capacity": 50})
        barrier = threading.Barrier(8)
        results, results_lock = [], threading.Lock()

        def worker():
            barrier.wait(timeout=10)
            result, _ = domain.submit_plan(
                Actor("teacher", "school", "sch-1"),
                {"plan_id": "plan-1", "school_id": "sch-1",
                 "window_id": "win-1", "student_count": 30},
                request_id="req-dup-1")
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 8)
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(domain.plans), 1)
        submitted = [e for e in domain.store.events if e["type"] == "plan_submitted"]
        self.assertEqual(len(submitted), 1)


if __name__ == "__main__":
    unittest.main()
