"""抽样规则:选用优先级、抽样量计算与等级评定。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fruit_trace import FakeClock, Grade, TraceSystem, testing
from fruit_trace.errors import NotFoundError


class SamplingRuleTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.system = TraceSystem(clock=self.clock)
        self.ids = testing.seed_base(self.system)

    def tearDown(self):
        self.system.close()

    def test_contract_rule_wins(self):
        rule = self.system.sampling.select_rule(
            testing.VARIETY, testing.SEASON, self.ids["contract_id"]
        )
        self.assertEqual(rule["id"], self.ids["rules"]["contract"])

    def test_variety_season_rule_beats_default(self):
        rule = self.system.sampling.select_rule(testing.VARIETY, testing.SEASON, None)
        self.assertEqual(rule["id"], self.ids["rules"]["variety_season"])

    def test_default_rule_when_nothing_specific(self):
        rule = self.system.sampling.select_rule("苹果", "其他产季", None)
        self.assertEqual(rule["id"], self.ids["rules"]["default"])

    def test_no_rule_raises(self):
        system = TraceSystem(clock=self.clock)
        try:
            with self.assertRaises(NotFoundError):
                system.sampling.select_rule("榴莲", "2026年夏季")
        finally:
            system.close()

    def test_sample_size_clamping(self):
        rule = self.system.registry.get_sampling_rule(self.ids["rules"]["contract"])
        # ratio=0.20, min=10, max=80
        self.assertEqual(self.system.sampling.sample_size(rule, 24), 10)  # 4.8 → 下限 10
        self.assertEqual(self.system.sampling.sample_size(rule, 1000), 80)  # 200 → 上限 80
        self.assertEqual(self.system.sampling.sample_size(rule, 3), 3)  # 不足时下限让位于总量

    def test_evaluate_grade_thresholds(self):
        rule = self.system.registry.get_sampling_rule(self.ids["rules"]["contract"])
        evaluate = self.system.sampling.evaluate_grade
        self.assertEqual(evaluate(rule, 0.005), Grade.A)
        self.assertEqual(evaluate(rule, 0.01), Grade.A)  # 边界含等于
        self.assertEqual(evaluate(rule, 0.02), Grade.B)
        self.assertEqual(evaluate(rule, 0.05), Grade.C)
        self.assertEqual(evaluate(rule, 0.06), Grade.C)
        self.assertEqual(evaluate(rule, 0.07), Grade.REJECT)


if __name__ == "__main__":
    unittest.main()
