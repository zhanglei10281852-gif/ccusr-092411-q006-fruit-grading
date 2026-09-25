"""审计:从任意箱号还原等级依据、结论变迁与客户通知。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fruit_trace import FakeClock, TraceSystem, testing
from fruit_trace.errors import NotFoundError
from fruit_trace.services.audit import format_audit_report

T0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(T0)
        self.system = TraceSystem(clock=self.clock)
        self.ids = testing.seed_base(self.system)
        self.lot_id = self.ids["lot_id"]
        # 完整业务轨迹:证据链 → 初检A → 有条件放行 → 交付一单 → 补录 → 复检REJECT
        testing.run_evidence_chain(self.system, self.ids)
        self.clock.advance(hours=1)
        testing.run_initial_inspection(self.system, self.ids)  # v1: A
        qa = self.ids["qa"]
        self.order1 = self.system.orders.create_order(self.lot_id, "客户甲", 300)
        self.order2 = self.system.orders.create_order(self.lot_id, "客户乙", 150)
        ruling = self.system.rulings.submit(self.lot_id, 1, "conditional", qa, "rk-1")
        self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=400, valid_until=self.clock.now() + timedelta(days=7), actor_id=qa,
        )
        self.system.releases.deliver_order(self.order1["id"])
        self.clock.advance(hours=2)
        # 补录一份发生在初检之前的留样检验
        self.system.inspections.record_inspection(
            self.lot_id, "initial", self.ids["inspector"],
            testing.sample_boxes(self.ids, 4), 400, 8,
            occurred_at=T0 + timedelta(minutes=30), note="留样补录",
        )
        self.clock.advance(days=1)
        testing.run_reinspection(self.system, self.ids, defects=120)  # v2: REJECT → 自动召回

    def tearDown(self):
        self.system.close()

    def test_audit_box_reconstructs_full_story(self):
        box_no = self.ids["box_nos"][0]
        report = self.system.audit.audit_box(box_no)

        self.assertEqual(report["box"]["box_no"], box_no)
        self.assertEqual(report["lot"]["code"], testing.LOT_CODE)
        self.assertEqual(report["container"]["container_no"], "CNTR-01")

        # 证据链完整(含客户复检环节)
        chain = report["evidence_chain"]
        self.assertTrue(chain["ok"])
        self.assertEqual(len(chain["events"]), 5)
        self.assertEqual(
            {e["actor_role"] for e in chain["events"]},
            {"origin_packer", "origin_sealer", "port_receiver", "market_inspector", "customer_inspector"},
        )

        # 等级为何成立:当前结论及其依据
        current = report["current_conclusion"]
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["grade"], "REJECT")
        self.assertEqual(current["basis"]["rule_name"], "鲜果汇合同专属规则")
        self.assertAlmostEqual(current["basis"]["defect_rate"], 0.12)
        self.assertIn("grade_thresholds", current["basis"])

        # 结论怎样变化:v1 A(已作废)→ v2 REJECT
        history = report["conclusion_history"]
        self.assertEqual([(c["version"], c["grade"], c["status"]) for c in history], [
            (1, "A", "superseded"),
            (2, "REJECT", "current"),
        ])
        self.assertIn("初检", history[0]["reason"])
        self.assertIn("复检推翻", history[1]["reason"])

        # 补录的检验归档在 v1,未改变当时结论
        backfilled = [i for i in report["inspections"] if i["backfilled"]]
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled[0]["conclusion_version"], 1)

        # 订单状态:已交付的被召回,未交付的被标记受影响
        orders = {o["customer_name"]: o["status"] for o in report["orders"]}
        self.assertEqual(orders["客户甲"], "recalled")
        self.assertEqual(orders["客户乙"], "affected")

        # 哪些客户已经收到通知
        notified = {(n["customer_name"], n["kind"]) for n in report["notifications"]}
        self.assertIn(("客户甲", "release_granted"), notified)
        self.assertIn(("客户甲", "recall"), notified)
        self.assertIn(("客户乙", "release_granted"), notified)
        self.assertIn(("客户乙", "order_affected"), notified)

        # 放行与召回记录
        self.assertEqual(len(report["releases"]), 1)
        self.assertEqual(report["releases"][0]["status"], "recalled")
        self.assertEqual(len(report["releases"][0]["recalls"]), 1)

    def test_audit_unknown_box(self):
        with self.assertRaises(NotFoundError):
            self.system.audit.audit_box("NO-SUCH-BOX")

    def test_format_report_renders_text(self):
        report = self.system.audit.audit_box(self.ids["box_nos"][0])
        text = format_audit_report(report)
        self.assertIn("箱号:", text)
        self.assertIn("结论变迁", text)
        self.assertIn("客户通知", text)


if __name__ == "__main__":
    unittest.main()
