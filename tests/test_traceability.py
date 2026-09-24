"""端到端业务规则测试。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from traceability import (
    AcceptanceRule,
    AuthorizationError,
    ConditionalReleaseError,
    GradeLevel,
    ImmutableHistoryError,
    QualityTraceService,
    ReleaseBlocked,
    SamplePlanViolation,
    SamplingRuleNotFound,
    Store,
    Role,
    Verdict,
    build_box_report,
    render_box_report,
)
from traceability.rules import RuleRegistry
from traceability.timeutil import parse


def make_rules() -> list[AcceptanceRule]:
    levels = (
        GradeLevel("A级", 0.05, Verdict.QUALIFIED),
        GradeLevel("B级", 0.10, Verdict.CONDITIONAL),
        GradeLevel("C级", 0.20, Verdict.REJECTED),
    )
    return [
        AcceptanceRule(
            rule_id="R-CN-GENERAL", version=1, commodity="*", season="*",
            contract_no="*", effective_from=parse("2026-01-01T00:00:00+08:00"),
            levels=levels, min_boxes=2, max_boxes=5, min_units=100,
            published_at=parse("2025-12-01T00:00:00+08:00"),
        ),
        AcceptanceRule(
            rule_id="R-CN-CONTRACT-X", version=1, commodity="车厘子", season="2026",
            contract_no="HT-X001", effective_from=parse("2026-06-01T00:00:00+08:00"),
            levels=levels, min_boxes=3, max_boxes=8, min_units=150,
            published_at=parse("2026-05-01T00:00:00+08:00"),
        ),
    ]


class Scenario:
    """构建一个已完成五环节、初检合格的标准批次。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.store = Store(path)
        self.svc = QualityTraceService(self.store)
        for uid, name, role in [
            ("u-pack", "产地装箱员张工", Role.ORIGIN_PACKER),
            ("u-seal", "承运人李工", Role.CARRIER_SEALER),
            ("u-port", "到港交接王工", Role.PORT_RECEIVER),
            ("u-market", "市场检验员赵工", Role.MARKET_INSPECTOR),
            ("u-cust", "客户检验员陈工", Role.CUSTOMER_INSPECTOR),
            ("u-qm", "质量经理孙总", Role.QUALITY_MANAGER),
            ("u-audit", "审计员周工", Role.AUDITOR),
        ]:
            self.svc.register_user(uid, name, role)
        for rule in make_rules():
            self.svc.publish_rule("u-qm", rule)
        self.lot = "LOT-2026-001"

    def build_green_lot(
        self,
        defective: int = 2,
        total: int = 100,
        contract: str = "HT-OTHER",
        found_seal: str | None = None,
        boxes: int = 4,
        submit_initial: bool = True,
        conclusion: str = "初检合格",
        recorded_at: str | None = None,
        sample_boxes: int | None = None,
    ) -> str:
        svc = self.svc
        t0 = "2026-09-10T08:00:00+08:00"
        svc.register_lot("u-pack", self.lot, "车厘子", "2026", contract,
                         boxes, t0, origin_grade_table="产地分级表2026版")
        for i in range(1, boxes + 1):
            svc.pack_box("u-pack", self.lot, f"BOX-{i:03d}", "A级",
                         f"2026-09-10T09:{i:02d}:00+08:00")
        svc.record_seal("u-seal", self.lot, "SEAL-888", "2026-09-11T10:00:00+08:00")
        svc.record_handover("u-port", self.lot, found_seal or "SEAL-888",
                            "2026-09-20T08:30:00+08:00")
        n_sampled = sample_boxes or min(3, boxes)
        svc.collect_sample("u-market", self.lot, 1, "initial",
                           [f"BOX-{i:03d}" for i in range(1, n_sampled + 1)],
                           total, "2026-09-20T10:00:00+08:00")
        if submit_initial:
            svc.submit_inspection("u-market", self.lot, 1, "initial",
                                  defective, total, "2026-09-20T11:00:00+08:00",
                                  conclusion=conclusion, recorded_at=recorded_at)
        return self.lot


class EvidenceChainTest(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_green_path_full_release(self):
        s = self.s
        s.build_green_lot(defective=2)
        event = s.svc.issue_decision(
            "u-qm", s.lot, "full", 1, "2026-09-20T12:00:00+08:00",
        )
        self.assertEqual(event.payload["release_type"], "full")
        lot = s.svc._lot(s.lot)
        self.assertEqual(lot.current_inspection.grade, "A级")

    def test_seal_mismatch_blocks_release(self):
        s = self.s
        s.build_green_lot(defective=2, found_seal="SEAL-999")
        with self.assertRaises(ReleaseBlocked) as ctx:
            s.svc.issue_decision("u-qm", s.lot, "full", 1,
                                 "2026-09-20T12:00:00+08:00")
        self.assertTrue(any("封识不符" in r for r in ctx.exception.reasons))

    def test_missing_handover_is_evidence_gap(self):
        s = self.s
        # 只走到封识，没有交接
        s.svc.register_lot("u-pack", s.lot, "车厘子", "2026", "HT-OTHER", 2,
                           "2026-09-10T08:00:00+08:00")
        s.svc.pack_box("u-pack", s.lot, "BOX-001", "A级", "2026-09-10T09:00:00+08:00")
        s.svc.pack_box("u-pack", s.lot, "BOX-002", "A级", "2026-09-10T09:05:00+08:00")
        s.svc.record_seal("u-seal", s.lot, "SEAL-1", "2026-09-11T10:00:00+08:00")
        from traceability.chain import verify_lot
        report = verify_lot(s.svc._lot(s.lot))
        self.assertFalse(report.clear)
        self.assertTrue(any("到港交接" in g for g in report.gaps))

    def test_broken_seal_recorded_blocks(self):
        s = self.s
        s.svc.register_lot("u-pack", s.lot, "车厘子", "2026", "HT-OTHER", 1,
                           "2026-09-10T08:00:00+08:00")
        s.svc.pack_box("u-pack", s.lot, "BOX-001", "A级",
                       "2026-09-10T09:00:00+08:00")
        s.svc.record_seal("u-seal", s.lot, "SEAL-1",
                          "2026-09-11T10:00:00+08:00", intact=False, note="施封时异常")
        s.svc.record_handover("u-port", s.lot, "SEAL-1",
                              "2026-09-20T08:30:00+08:00")
        from traceability.chain import verify_lot
        report = verify_lot(s.svc._lot(s.lot))
        self.assertFalse(report.seal_ok)


class RoleSeparationTest(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_each_stage_requires_its_own_role(self):
        s = self.s
        with self.assertRaises(AuthorizationError):
            s.svc.register_lot("u-market", "LOT-X", "车厘子", "2026", "HT", 1,
                               "2026-09-10T08:00:00+08:00")
        s.svc.register_lot("u-pack", "LOT-X", "车厘子", "2026", "HT", 1,
                           "2026-09-10T08:00:00+08:00")
        with self.assertRaises(AuthorizationError):
            s.svc.pack_box("u-seal", "LOT-X", "BOX-1", "A级",
                           "2026-09-10T09:00:00+08:00")
        s.svc.pack_box("u-pack", "LOT-X", "BOX-1", "A级",
                       "2026-09-10T09:00:00+08:00")
        with self.assertRaises(AuthorizationError):
            s.svc.record_seal("u-port", "LOT-X", "SEAL-1",
                              "2026-09-11T10:00:00+08:00")
        # 审计员任何写环节都不能签署
        with self.assertRaises(AuthorizationError):
            s.svc.record_seal("u-audit", "LOT-X", "SEAL-1",
                              "2026-09-11T10:00:00+08:00")

    def test_market_inspector_cannot_do_customer_reinspection(self):
        s = self.s
        s.build_green_lot()
        s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                             ["BOX-001", "BOX-002"], 100,
                             "2026-09-21T10:00:00+08:00")
        with self.assertRaises(AuthorizationError):
            s.svc.submit_inspection("u-market", s.lot, 2, "reinspection",
                                    5, 100, "2026-09-21T11:00:00+08:00")


class SamplingRuleTest(unittest.TestCase):
    def test_select_by_commodity_season_contract_and_time(self):
        registry = RuleRegistry()
        for rule in make_rules():
            registry.publish(rule)
        # 合同专用规则优先
        chosen = registry.select("车厘子", "2026", "HT-X001",
                                 "2026-09-01T00:00:00+08:00")
        self.assertEqual(chosen.rule_id, "R-CN-CONTRACT-X")
        self.assertEqual(chosen.min_units, 150)
        # 其他合同回落到通用规则
        chosen = registry.select("车厘子", "2026", "HT-OTHER",
                                 "2026-09-01T00:00:00+08:00")
        self.assertEqual(chosen.rule_id, "R-CN-GENERAL")
        # 任何规则生效日之前找不到规则
        with self.assertRaises(SamplingRuleNotFound):
            registry.select("车厘子", "2026", "HT-X001",
                            "2025-12-31T00:00:00+08:00")

    def test_published_rule_is_immutable(self):
        registry = RuleRegistry()
        (rule,) = [r for r in make_rules() if r.version == 1 and r.rule_id == "R-CN-GENERAL"]
        registry.publish(rule)
        with self.assertRaises(ValueError):
            registry.publish(rule)

    def test_sample_size_violation_rejected(self):
        s = Scenario()
        s.svc.register_lot("u-pack", s.lot, "车厘子", "2026", "HT-X001", 4,
                           "2026-09-10T08:00:00+08:00")
        for i in range(1, 5):
            s.svc.pack_box("u-pack", s.lot, f"BOX-{i:03d}", "A级",
                           f"2026-09-10T09:0{i}:00+08:00")
        s.svc.record_seal("u-seal", s.lot, "SEAL-888", "2026-09-11T10:00:00+08:00")
        s.svc.record_handover("u-port", s.lot, "SEAL-888",
                              "2026-09-20T08:30:00+08:00")
        # 合同规则要求至少 3 箱
        with self.assertRaises(SamplePlanViolation):
            s.svc.collect_sample("u-market", s.lot, 1, "initial",
                                 ["BOX-001", "BOX-002"], 150,
                                 "2026-09-20T10:00:00+08:00")

    def test_sample_box_must_belong_to_lot(self):
        s = Scenario()
        s.build_green_lot()
        with self.assertRaises(SamplePlanViolation):
            s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                                 ["BOX-001", "BOX-999"], 100,
                                 "2026-09-21T10:00:00+08:00")


class BackfillAndVersionTest(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_backfill_preserves_conclusion_and_uses_rule_at_business_time(self):
        s = self.s
        # 业务发生在 9 月 20 日，9 月 25 日才补录检验结论
        s.build_green_lot(
            defective=8, submit_initial=False,
        )
        event = s.svc.submit_inspection(
            "u-market", s.lot, 1, "backfill-market", 8, 100,
            "2026-09-20T11:00:00+08:00",
            conclusion="当时判定：B级，按样品原样保留",
            recorded_at="2026-09-25T15:00:00+08:00",
        )
        self.assertTrue(event.payload["backfilled"])
        lot = s.svc._lot(s.lot)
        record = lot.inspections[1]
        # 当时结论逐字保留，按业务发生时的规则定级（8% → B级）
        self.assertEqual(record.grade, "B级")
        self.assertEqual(record.conclusion, "当时判定：B级，按样品原样保留")
        self.assertEqual(record.event.recorded_at, "2026-09-25T15:00:00+08:00")
        # 事件本身永不删除、不可改，且按业务时间保留在第 1 轮
        events = s.store.events_for(s.lot)
        self.assertTrue(all(e.seq is not None for e in events))
        self.assertEqual(lot.current_round, 1)

    def test_reinspection_creates_new_version_and_marks_only_undelivered(self):
        s = self.s
        s.build_green_lot(defective=2)  # A级
        s.svc.issue_decision("u-qm", s.lot, "full", 1,
                             "2026-09-20T12:00:00+08:00")
        s.svc.register_order("u-qm", s.lot, "PO-A", "客户甲", 2)
        s.svc.register_order("u-qm", s.lot, "PO-B", "客户乙", 2)
        # PO-A 已交付，PO-B 未交付
        s.svc.mark_delivered("u-qm", s.lot, "PO-A", 2,
                             "2026-09-20T15:00:00+08:00")
        # 客户复检：不合格率 15% → 推翻为 C级/rejected
        s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                             ["BOX-001", "BOX-002"], 100,
                             "2026-09-22T10:00:00+08:00")
        s.svc.submit_inspection("u-cust", s.lot, 2, "reinspection",
                                15, 100, "2026-09-22T11:00:00+08:00",
                                conclusion="复检不合格，整批拒收")
        lot = s.svc._lot(s.lot)
        self.assertEqual([r.grade for r in lot.grade_history()], ["A级", "C级"])
        self.assertTrue(lot.inspections[2].is_reinspection)
        self.assertEqual(lot.inspections[2].supersedes_round, 1)
        # 只有未交付的 PO-B 收到改判通知
        notices_a = [e for e in lot.orders["PO-A"].notices]
        notices_b = [e for e in lot.orders["PO-B"].notices]
        self.assertTrue(all(n.payload["kind"] != "overturn" for n in notices_a))
        self.assertTrue(any(n.payload["kind"] == "overturn" for n in notices_b))

    def test_same_grade_reinspection_is_not_overturn(self):
        s = self.s
        s.build_green_lot(defective=2)
        s.svc.register_order("u-qm", s.lot, "PO-A", "客户甲", 4)
        s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                             ["BOX-001", "BOX-002"], 100,
                             "2026-09-22T10:00:00+08:00")
        s.svc.submit_inspection("u-cust", s.lot, 2, "reinspection",
                                3, 100, "2026-09-22T11:00:00+08:00")
        lot = s.svc._lot(s.lot)
        self.assertTrue(lot.inspections[2].is_reinspection)
        self.assertFalse(
            any(n.payload["kind"] == "overturn"
                for o in lot.orders.values() for n in o.notices)
        )


class ConcurrentVerdictTest(unittest.TestCase):
    def test_two_inspectors_submit_concurrently_only_one_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "trace.db"
            s = Scenario(db)
            s.build_green_lot(defective=2)
            # 复检轮：两个客户检验员并发提交
            s.svc.register_user("u-cust2", "客户检验员林工", Role.CUSTOMER_INSPECTOR)
            s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                                 ["BOX-001", "BOX-002"], 100,
                                 "2026-09-22T10:00:00+08:00")
            errors: list[Exception] = []

            def submit(uid: str):
                local = QualityTraceService(Store(db))
                try:
                    local.submit_inspection(
                        uid, s.lot, 2, "reinspection", 4, 100,
                        "2026-09-22T11:00:00+08:00",
                        event_id=f"evt-concurrent-{uid}",
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=submit, args=("u-cust",))
            t2 = threading.Thread(target=submit, args=("u-cust2",))
            t1.start(); t2.start(); t1.join(); t2.join()

            lot = s.svc._lot(s.lot)
            self.assertEqual(len(lot.inspections), 2)  # 只有初检 + 一次复检
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ImmutableHistoryError)


class ConditionalReleaseTest(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def _conditional_lot(self):
        s = self.s
        s.build_green_lot(defective=8)  # 8% → B级 / conditional
        return s.svc.issue_decision(
            "u-qm", s.lot, "conditional", 1, "2026-09-20T12:00:00+08:00",
            qty_limit=2, valid_until="2026-09-30T23:59:59+08:00",
            reason="降级限量试销",
        )

    def test_conditional_requires_qty_and_validity(self):
        s = self.s
        s.build_green_lot(defective=8)
        with self.assertRaises(ConditionalReleaseError):
            s.svc.issue_decision("u-qm", s.lot, "conditional", 1,
                                 "2026-09-20T12:00:00+08:00",
                                 valid_until="2026-09-30T23:59:59+08:00")
        with self.assertRaises(ConditionalReleaseError):
            s.svc.issue_decision("u-qm", s.lot, "conditional", 1,
                                 "2026-09-20T12:00:00+08:00", qty_limit=2)

    def test_rejected_grade_cannot_be_conditionally_released(self):
        s = self.s
        s.build_green_lot(defective=18)  # 18% → C级 / rejected
        with self.assertRaises(ReleaseBlocked):
            s.svc.issue_decision("u-qm", s.lot, "conditional", 1,
                                 "2026-09-20T12:00:00+08:00",
                                 qty_limit=2,
                                 valid_until="2026-09-30T23:59:59+08:00")

    def test_qty_limit_enforced_on_delivery(self):
        s = self.s
        self._conditional_lot()
        s.svc.register_order("u-qm", s.lot, "PO-A", "客户甲", 2)
        s.svc.register_order("u-qm", s.lot, "PO-B", "客户乙", 2)
        s.svc.mark_delivered("u-qm", s.lot, "PO-A", 2,
                             "2026-09-21T09:00:00+08:00")
        with self.assertRaises(ConditionalReleaseError):
            s.svc.mark_delivered("u-qm", s.lot, "PO-B", 1,
                                 "2026-09-22T09:00:00+08:00")

    def test_delivery_after_expiry_blocked_and_recall_on_expiry(self):
        s = self.s
        decision = self._conditional_lot()
        s.svc.register_order("u-qm", s.lot, "PO-A", "客户甲", 2)
        with self.assertRaises(ReleaseBlocked):
            s.svc.mark_delivered("u-qm", s.lot, "PO-A", 1,
                                 "2026-10-01T09:00:00+08:00")
        # 期满可召回
        s.svc.recall_decision("u-qm", s.lot, decision.payload["decision_no"],
                              "有条件放行期满未消化，召回",
                              "2026-10-01T10:00:00+08:00")
        lot = s.svc._lot(s.lot)
        self.assertIsNone(lot.effective_decision())
        self.assertTrue(lot.decisions[0].recalled)

    def test_recall_on_subsequent_failure(self):
        s = self.s
        decision = self._conditional_lot()
        s.svc.register_order("u-qm", s.lot, "PO-A", "客户甲", 2)
        # 有效期内客户复检判拒收
        s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                             ["BOX-001", "BOX-002"], 100,
                             "2026-09-25T10:00:00+08:00")
        s.svc.submit_inspection("u-cust", s.lot, 2, "reinspection",
                                16, 100, "2026-09-25T11:00:00+08:00")
        s.svc.recall_decision("u-qm", s.lot, decision.payload["decision_no"],
                              "后续复检不合格，立即召回",
                              "2026-09-25T12:00:00+08:00")
        lot = s.svc._lot(s.lot)
        self.assertTrue(lot.decisions[0].recall_event.payload["trigger"]
                        == "subsequent_failure")

    def test_recall_without_reason_disallowed(self):
        s = self.s
        decision = self._conditional_lot()
        from traceability.errors import DomainError
        with self.assertRaises(DomainError):
            s.svc.recall_decision("u-qm", s.lot, decision.payload["decision_no"],
                                  "无故召回", "2026-09-22T10:00:00+08:00")


class AuditTest(unittest.TestCase):
    def test_audit_from_box_number_tells_full_story(self):
        s = Scenario()
        s.build_green_lot(defective=2)
        s.svc.register_order("u-qm", s.lot, "PO-B", "客户乙", 2)
        s.svc.issue_decision("u-qm", s.lot, "full", 1,
                             "2026-09-20T12:00:00+08:00")
        s.svc.collect_sample("u-cust", s.lot, 2, "reinspection",
                             ["BOX-001", "BOX-002"], 100,
                             "2026-09-22T10:00:00+08:00")
        s.svc.submit_inspection("u-cust", s.lot, 2, "reinspection",
                                15, 100, "2026-09-22T11:00:00+08:00",
                                conclusion="复检不合格")
        report = build_box_report(s.store, "BOX-002")
        self.assertEqual(report["lot_no"], s.lot)
        self.assertTrue(report["evidence_chain"]["clear"])
        self.assertEqual([v["round"] for v in report["grade_versions"]], [1, 2])
        self.assertTrue(report["grade_versions"][1]["changed_from_previous"])
        # 未交付订单 PO-B 同时收到放行与改判通知
        kinds = {n["kind"] for n in report["customer_notices"] if n["order_no"] == "PO-B"}
        self.assertIn("release", kinds)
        self.assertIn("overturn", kinds)
        text = render_box_report(report)
        self.assertIn("BOX-002", text)
        self.assertIn("改判", text)

    def test_audit_unknown_box_raises(self):
        s = Scenario()
        with self.assertRaises(KeyError):
            build_box_report(s.store, "BOX-NOPE")


if __name__ == "__main__":
    unittest.main()
