"""放行:阻断条件、有条件放行的限量与有效期、到期与召回。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fruit_trace import FakeClock, Stage, TraceSystem, testing
from fruit_trace.errors import (
    DomainError,
    PermissionDeniedError,
    ReleaseBlockedError,
    ValidationError,
)

T0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(T0)
        self.system = TraceSystem(clock=self.clock)
        self.ids = testing.seed_base(self.system)
        self.lot_id = self.ids["lot_id"]
        self.qa = self.ids["qa"]

    def tearDown(self):
        self.system.close()

    def _ready_lot(self, **chain_kwargs):
        """证据链完整且已有初检结论(A)的批次。"""
        testing.run_evidence_chain(self.system, self.ids, **chain_kwargs)
        testing.run_initial_inspection(self.system, self.ids)

    def _rule(self, outcome="conditional", key="rk-1"):
        return self.system.rulings.submit(self.lot_id, 1, outcome, self.qa, key)

    def test_release_blocked_by_evidence_gap(self):
        # 只签署装箱与封识,缺少交接与抽检
        self.system.evidence.record_event(
            self.lot_id, Stage.PACKING, self.ids["packer"], testing.packing_payload(self.ids)
        )
        self.system.evidence.record_event(
            self.lot_id, Stage.SEALING, self.ids["sealer"], testing.sealing_payload(self.ids)
        )
        ruling = self._rule()
        with self.assertRaises(ReleaseBlockedError) as ctx:
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "conditional",
                max_quantity=100, valid_until=self.clock.now() + timedelta(days=7),
                actor_id=self.qa,
            )
        reasons = ctx.exception.reasons
        self.assertTrue(any("port_handover" in r for r in reasons))
        self.assertTrue(any("market_sampling" in r for r in reasons))
        self.assertTrue(any("缺少等级结论" in r for r in reasons))

    def test_release_blocked_by_seal_mismatch(self):
        self._ready_lot(tamper_seal=True)
        ruling = self._rule()
        with self.assertRaises(ReleaseBlockedError) as ctx:
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "conditional",
                max_quantity=100, valid_until=self.clock.now() + timedelta(days=7),
                actor_id=self.qa,
            )
        self.assertTrue(any("封识号不符" in r for r in ctx.exception.reasons))

    def test_release_blocked_without_conclusion(self):
        testing.run_evidence_chain(self.system, self.ids)  # 有证据链但无检验结论
        ruling = self._rule()
        with self.assertRaises(ReleaseBlockedError) as ctx:
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "full", actor_id=self.qa
            )
        self.assertTrue(any("缺少等级结论" in r for r in ctx.exception.reasons))

    def test_release_blocked_when_reject(self):
        testing.run_evidence_chain(self.system, self.ids)
        testing.run_initial_inspection(self.system, self.ids, defects=120)  # REJECT
        ruling = self._rule("reject")
        with self.assertRaises(ReleaseBlockedError) as ctx:
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "full", actor_id=self.qa
            )
        self.assertTrue(any("不合格" in r for r in ctx.exception.reasons))

    def test_conditional_ruling_cannot_release_fully(self):
        self._ready_lot()
        ruling = self._rule("conditional")
        with self.assertRaises(ReleaseBlockedError):
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "full", actor_id=self.qa
            )

    def test_conditional_release_requires_quantity_and_validity(self):
        self._ready_lot()
        ruling = self._rule()
        with self.assertRaises(ValidationError):
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "conditional",
                valid_until=self.clock.now() + timedelta(days=7), actor_id=self.qa,
            )
        with self.assertRaises(ValidationError):
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "conditional",
                max_quantity=100, actor_id=self.qa,
            )
        with self.assertRaises(ValidationError):
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "conditional",
                max_quantity=100, valid_until=self.clock.now() - timedelta(hours=1),
                actor_id=self.qa,
            )

    def test_conditional_release_limits_delivery_quantity(self):
        self._ready_lot()
        ruling = self._rule()
        self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=120, valid_until=self.clock.now() + timedelta(days=7),
            actor_id=self.qa,
        )
        order1 = self.system.orders.create_order(self.lot_id, "客户甲", 100)
        order2 = self.system.orders.create_order(self.lot_id, "客户乙", 50)
        self.system.releases.deliver_order(order1["id"])
        with self.assertRaises(ValidationError) as ctx:
            self.system.releases.deliver_order(order2["id"])
        self.assertIn("超出有条件放行数量限制", str(ctx.exception))

    def test_delivery_requires_active_release(self):
        self._ready_lot()
        order = self.system.orders.create_order(self.lot_id, "客户甲", 10)
        with self.assertRaises(ReleaseBlockedError):
            self.system.releases.deliver_order(order["id"])

    def test_expired_release_blocks_delivery(self):
        self._ready_lot()
        ruling = self._rule()
        release = self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=100, valid_until=self.clock.now() + timedelta(days=7),
            actor_id=self.qa,
        )
        order = self.system.orders.create_order(self.lot_id, "客户甲", 10)
        self.clock.advance(days=8)
        with self.assertRaises(ReleaseBlockedError) as ctx:
            self.system.releases.deliver_order(order["id"])
        self.assertTrue(any("已过有效期" in r for r in ctx.exception.reasons))
        self.assertEqual(self.system.releases.get_release(release["id"])["status"], "expired")

    def test_expire_overdue_marks_releases(self):
        self._ready_lot()
        ruling = self._rule()
        self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=100, valid_until=self.clock.now() + timedelta(days=7),
            actor_id=self.qa,
        )
        self.clock.advance(days=8)
        expired = self.system.releases.expire_overdue()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["status"], "expired")

    def test_recall_marks_delivered_and_pending_orders(self):
        self._ready_lot()
        ruling = self._rule()
        release = self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=200, valid_until=self.clock.now() + timedelta(days=7),
            actor_id=self.qa,
        )
        order1 = self.system.orders.create_order(self.lot_id, "客户甲", 100)
        order2 = self.system.orders.create_order(self.lot_id, "客户乙", 50)
        self.system.releases.deliver_order(order1["id"])

        self.system.releases.recall(release["id"], "市场监测发现农残超标", actor_id=self.qa)

        self.assertEqual(self.system.releases.get_release(release["id"])["status"], "recalled")
        self.assertEqual(self.system.orders.get(order1["id"])["status"], "recalled")
        self.assertEqual(self.system.orders.get(order2["id"])["status"], "affected")
        report = self.system.audit.audit_box(self.ids["box_nos"][0])
        kinds = {(n["customer_name"], n["kind"]) for n in report["notifications"]}
        self.assertIn(("客户甲", "recall"), kinds)
        self.assertIn(("客户乙", "order_affected"), kinds)
        # 重复召回被拒绝
        with self.assertRaises(ValidationError):
            self.system.releases.recall(release["id"], "再次召回", actor_id=self.qa)

    def test_expired_conditional_release_can_be_recalled(self):
        self._ready_lot()
        ruling = self._rule()
        release = self.system.releases.request_release(
            self.lot_id, ruling["id"], "conditional",
            max_quantity=200, valid_until=self.clock.now() + timedelta(days=7),
            actor_id=self.qa,
        )
        order = self.system.orders.create_order(self.lot_id, "客户甲", 100)
        self.system.releases.deliver_order(order["id"])
        self.clock.advance(days=8)
        self.system.releases.expire_overdue()
        self.system.releases.recall(release["id"], "期满后市场复检不合格", actor_id=self.qa)
        self.assertEqual(self.system.orders.get(order["id"])["status"], "recalled")

    def test_only_one_active_release_per_lot(self):
        self._ready_lot()
        ruling = self._rule("pass")
        self.system.releases.request_release(
            self.lot_id, ruling["id"], "full", actor_id=self.qa
        )
        ruling2 = self.system.rulings.submit(self.lot_id, 2, "pass", self.qa, "rk-2")
        with self.assertRaises(DomainError):
            self.system.releases.request_release(
                self.lot_id, ruling2["id"], "full", actor_id=self.qa
            )

    def test_release_requires_qa_role(self):
        self._ready_lot()
        ruling = self._rule()
        with self.assertRaises(PermissionDeniedError):
            self.system.releases.request_release(
                self.lot_id, ruling["id"], "full", actor_id=self.ids["inspector"]
            )

    def test_full_release_happy_path(self):
        self._ready_lot()
        ruling = self._rule("pass")
        release = self.system.releases.request_release(
            self.lot_id, ruling["id"], "full", actor_id=self.qa
        )
        self.assertEqual(release["status"], "active")
        lot = self.system.registry.get_lot(self.lot_id)
        self.assertEqual(lot["state"], "released")
        order = self.system.orders.create_order(self.lot_id, "客户甲", 10)
        delivered = self.system.releases.deliver_order(order["id"])
        self.assertEqual(delivered["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
