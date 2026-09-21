"""并发占位容量验收：高并发下绝不超额。"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from service.models import ApiError
from tests.helpers import CONTROL, make_service, seed_world, submit_approved_plan


class CapacityConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.app = make_service()
        seed_world(self.app, capacity=6, signal=100)  # 受校区容量 6 席限制

    def test_concurrent_holds_never_exceed_capacity(self):
        # 4 个计划各占 2 席，共需 8 席，而容量仅 6 席 → 恰好 3 个成功。
        for i in range(4):
            submit_approved_plan(self.app, f"school-A", f"plan-c{i}", 2)

        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def hold(plan_id):
            barrier.wait()  # 尽量让四个线程同时进入
            try:
                self.app.hold_plan(CONTROL, plan_id)
                ok = True
            except ApiError as err:
                self.assertEqual(err.code, "CAPACITY_EXCEEDED")
                ok = False
            with lock:
                results.append(ok)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(hold, [f"plan-c{i}" for i in range(4)]))

        self.assertEqual(sum(results), 3)
        self.assertEqual(self.app._used_seats("w1"), 6)

    def test_release_allows_rebooking_after_capacity_denial(self):
        submit_approved_plan(self.app, "school-A", "plan-x", 6)
        submit_approved_plan(self.app, "school-B", "plan-y", 1)
        self.app.hold_plan(CONTROL, "plan-x")
        with self.assertRaises(ApiError) as cm:
            self.app.hold_plan(CONTROL, "plan-y")
        self.assertEqual(cm.exception.code, "CAPACITY_EXCEEDED")
        # 退出释放 6 席后，被拒计划可重新占位成功。
        self.app.withdraw_plan(CONTROL, "plan-x", "学校退出")
        self.app.hold_plan(CONTROL, "plan-y")
        self.assertEqual(self.app._used_seats("w1"), 1)

    def test_signal_capacity_is_also_enforced(self):
        app = make_service()
        seed_world(app, capacity=100, signal=3)
        submit_approved_plan(app, "school-A", "plan-s1", 2)
        submit_approved_plan(app, "school-B", "plan-s2", 2)
        app.hold_plan(CONTROL, "plan-s1")
        with self.assertRaises(ApiError) as cm:
            app.hold_plan(CONTROL, "plan-s2")
        self.assertEqual(cm.exception.code, "CAPACITY_EXCEEDED")
        self.assertEqual(cm.exception.extra["used"], 2)
        self.assertEqual(cm.exception.extra["capacity"], 3)


    def test_shrink_before_lock_blocks_overcapacity_lock(self):
        app = make_service()
        seed_world(app, capacity=10, signal=10)
        submit_approved_plan(app, "school-A", "plan-p1", 5)
        app.hold_plan(CONTROL, "plan-p1")
        # 锁定前窗口缩到 3 席：不允许把 5 席旧占位超额锁进新窗口。
        app.revise_window(
            CONTROL, "w1", "2026-10-01T09:00:00Z", "2026-10-01T09:10:00Z",
            3, "锁定前缩短", expected_version=0)
        with self.assertRaises(ApiError) as cm:
            app.lock_window(CONTROL, "w1", "尝试锁定")
        self.assertEqual(cm.exception.code, "CAPACITY_EXCEEDED")
        self.assertEqual(cm.exception.extra, {"used": 5, "capacity": 3})


if __name__ == "__main__":
    unittest.main()
