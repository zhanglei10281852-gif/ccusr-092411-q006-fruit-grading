"""检验与结论版本化:补录保留当时结论、复检推翻产生新版本、仅标记未交付订单。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fruit_trace import FakeClock, TraceSystem, testing
from fruit_trace.errors import EvidenceGapError, PermissionDeniedError, ValidationError

T0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


class InspectionVersionTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(T0)
        self.system = TraceSystem(clock=self.clock)
        self.ids = testing.seed_base(self.system)
        self.lot_id = self.ids["lot_id"]
        testing.run_evidence_chain(self.system, self.ids)

    def tearDown(self):
        self.system.close()

    def _conclusions(self):
        return self.system.inspections.conclusions_for_lot(self.lot_id)

    def test_initial_inspection_creates_version_1(self):
        self.clock.advance(hours=1)
        inspection = testing.run_initial_inspection(self.system, self.ids)
        self.assertEqual(inspection["grade"], "A")
        self.assertEqual(inspection["conclusion_version"], 1)
        self.assertFalse(inspection["backfilled"])
        conclusions = self._conclusions()
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0]["status"], "current")
        self.assertEqual(conclusions[0]["basis"]["rule_name"], "鲜果汇合同专属规则")

    def test_backfilled_sample_preserves_standing_conclusion(self):
        self.clock.advance(hours=1)
        testing.run_initial_inspection(self.system, self.ids)  # v1: A,created_at = T0+1h
        self.clock.advance(hours=2)
        # 补录:样本在 v1 生成之前(初检同一轮)采集,现在才录入;即使数据不同也不改当时结论
        late = self.system.inspections.record_inspection(
            self.lot_id, "initial", self.ids["inspector"],
            testing.sample_boxes(self.ids, 4), total_fruits=400, defect_fruits=20,  # 5% → C
            occurred_at=T0 + timedelta(minutes=30),
            note="产地留样补录",
        )
        self.assertTrue(late["backfilled"])
        self.assertEqual(late["conclusion_version"], 1)
        conclusions = self._conclusions()
        self.assertEqual(len(conclusions), 1, "补录不得产生新版本")
        self.assertEqual(conclusions[0]["grade"], "A", "当时结论必须保留")

    def test_backfill_attaches_to_historical_version(self):
        self.clock.advance(hours=1)
        testing.run_initial_inspection(self.system, self.ids)  # v1 @ T0+1h
        self.clock.advance(hours=1)
        testing.run_reinspection(self.system, self.ids, defects=120)  # v2: REJECT @ T0+2h
        self.clock.advance(hours=1)
        # 补录一份发生在 v1 与 v2 之间的检验
        late = self.system.inspections.record_inspection(
            self.lot_id, "initial", self.ids["inspector"],
            testing.sample_boxes(self.ids, 4), total_fruits=400, defect_fruits=4,
            occurred_at=T0 + timedelta(hours=1, minutes=30),
        )
        self.assertTrue(late["backfilled"])
        self.assertEqual(late["conclusion_version"], 1, "应归档到发生时刻生效的 v1")
        self.assertEqual(len(self._conclusions()), 2, "补录不得产生新版本")

    def test_reinspection_overturn_creates_new_version(self):
        testing.run_initial_inspection(self.system, self.ids)  # v1: A
        self.clock.advance(days=2)
        testing.run_reinspection(self.system, self.ids, defects=50)  # 5% → C
        conclusions = self._conclusions()
        self.assertEqual(len(conclusions), 2)
        self.assertEqual(conclusions[0]["status"], "superseded")
        self.assertEqual(conclusions[1]["status"], "current")
        self.assertEqual(conclusions[1]["grade"], "C")
        self.assertIn("复检推翻", conclusions[1]["reason"])

    def test_only_undelivered_orders_marked_affected(self):
        testing.run_initial_inspection(self.system, self.ids)  # v1: A
        qa = self.ids["qa"]
        ruling = self.system.rulings.submit(self.lot_id, 1, "pass", qa, "rk-1")
        self.system.releases.request_release(
            self.lot_id, ruling["id"], "full", actor_id=qa
        )
        order1 = self.system.orders.create_order(self.lot_id, "客户甲", 100)
        order2 = self.system.orders.create_order(self.lot_id, "客户乙", 50)
        self.system.releases.deliver_order(order1["id"])  # 客户甲已交付

        self.clock.advance(days=1)
        testing.run_reinspection(self.system, self.ids, defects=50)  # A → C 降级

        order1 = self.system.orders.get(order1["id"])
        order2 = self.system.orders.get(order2["id"])
        self.assertEqual(order1["status"], "delivered", "已交付订单不被标记受影响")
        self.assertEqual(order2["status"], "affected", "未交付订单应被标记受影响")
        self.assertEqual(order2["affected_by_version"], 2)
        notices = self.system.audit.audit_box(self.ids["box_nos"][0])["notifications"]
        affected = [n for n in notices if n["kind"] == "order_affected"]
        self.assertEqual({n["customer_name"] for n in affected}, {"客户乙"})

    def test_same_grade_reinspection_keeps_version(self):
        testing.run_initial_inspection(self.system, self.ids)  # v1: A
        self.clock.advance(days=1)
        again = testing.run_reinspection(self.system, self.ids, defects=8)  # 0.8% → A
        self.assertEqual(again["conclusion_version"], 1)
        self.assertEqual(len(self._conclusions()), 1, "结论一致不产生新版本")

    def test_upgrade_reinspection_marks_no_orders(self):
        testing.run_initial_inspection(self.system, self.ids, defects=40)  # 4% → C
        order = self.system.orders.create_order(self.lot_id, "客户甲", 100)
        self.clock.advance(days=1)
        testing.run_reinspection(self.system, self.ids, defects=5)  # 0.5% → A 升级
        self.assertEqual(len(self._conclusions()), 2)
        self.assertEqual(self.system.orders.get(order["id"])["status"], "pending")

    def test_reject_reinspection_auto_recalls_active_release(self):
        testing.run_initial_inspection(self.system, self.ids)  # v1: A
        qa = self.ids["qa"]
        ruling = self.system.rulings.submit(self.lot_id, 1, "conditional", qa, "rk-1")
        release = self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=400, valid_until=self.clock.now() + timedelta(days=7), actor_id=qa,
        )
        order = self.system.orders.create_order(self.lot_id, "客户甲", 100)
        self.system.releases.deliver_order(order["id"])

        self.clock.advance(days=2)
        testing.run_reinspection(self.system, self.ids, defects=120)  # 12% → REJECT

        self.assertEqual(self.system.releases.get_release(release["id"])["status"], "recalled")
        self.assertEqual(self.system.orders.get(order["id"])["status"], "recalled")
        notices = self.system.audit.audit_box(self.ids["box_nos"][0])["notifications"]
        self.assertTrue(any(n["kind"] == "recall" and n["customer_name"] == "客户甲" for n in notices))

    def test_inspection_requires_sampling_evidence(self):
        lot2 = self.system.registry.register_lot("LOT-X", "车厘子", "2026年春季", {"C-1": 4})
        with self.assertRaises(EvidenceGapError):
            self.system.inspections.record_inspection(
                lot2["id"], "initial", self.ids["inspector"], ["LOT-X-C-1-0001"], 100, 1
            )

    def test_inspection_samples_must_match_evidence(self):
        with self.assertRaises(ValidationError):
            self.system.inspections.record_inspection(
                self.lot_id, "initial", self.ids["inspector"],
                ["LOT-2026-0001-CNTR-01-0012"], 100, 1,  # 不在抽样证据(前10箱)中
            )

    def test_inspection_role_enforced(self):
        with self.assertRaises(PermissionDeniedError):
            self.system.inspections.record_inspection(
                self.lot_id, "initial", self.ids["reinspector"],
                testing.sample_boxes(self.ids), 100, 1,
            )


if __name__ == "__main__":
    unittest.main()
