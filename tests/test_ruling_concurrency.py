"""放行裁决:并发提交只形成一次有效裁决,幂等键保证重试安全。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone

from fruit_trace import FakeClock, TraceSystem, testing
from fruit_trace.errors import PermissionDeniedError, RulingConflictError


class RulingConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmp.name}/trace.db"
        self.clock = FakeClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        system = TraceSystem(self.db_path, clock=self.clock)
        self.ids = testing.seed_base(system)
        system.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_submissions_single_winner(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit(i):
            system = TraceSystem(self.db_path, clock=self.clock)
            try:
                barrier.wait(timeout=10)
                ruling = system.rulings.submit(
                    self.ids["lot_id"], 1, "pass", self.ids["qa"], f"key-{i}"
                )
                results.append(ruling)
            except RulingConflictError:
                errors.append(i)
            finally:
                system.close()

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 1, "并发提交只能形成一个有效裁决")
        self.assertEqual(len(errors), 1)

        system = TraceSystem(self.db_path, clock=self.clock)
        try:
            rulings = system.rulings.list_for_lot(self.ids["lot_id"])
            self.assertEqual(len(rulings), 1)
        finally:
            system.close()

    def test_idempotent_retry_returns_same_ruling(self):
        system = TraceSystem(self.db_path, clock=self.clock)
        try:
            first = system.rulings.submit(
                self.ids["lot_id"], 1, "pass", self.ids["qa"], "retry-key"
            )
            second = system.rulings.submit(
                self.ids["lot_id"], 1, "pass", self.ids["qa"], "retry-key"
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(system.rulings.list_for_lot(self.ids["lot_id"])), 1)
        finally:
            system.close()

    def test_same_round_different_key_conflicts(self):
        system = TraceSystem(self.db_path, clock=self.clock)
        try:
            system.rulings.submit(self.ids["lot_id"], 1, "pass", self.ids["qa"], "key-a")
            with self.assertRaises(RulingConflictError):
                system.rulings.submit(self.ids["lot_id"], 1, "reject", self.ids["qa"], "key-b")
            # 新一轮次可以再次裁决
            second = system.rulings.submit(self.ids["lot_id"], 2, "reject", self.ids["qa"], "key-c")
            self.assertEqual(second["round"], 2)
        finally:
            system.close()

    def test_only_qa_manager_can_rule(self):
        system = TraceSystem(self.db_path, clock=self.clock)
        try:
            with self.assertRaises(PermissionDeniedError):
                system.rulings.submit(
                    self.ids["lot_id"], 1, "pass", self.ids["inspector"], "key-x"
                )
        finally:
            system.close()


if __name__ == "__main__":
    unittest.main()
