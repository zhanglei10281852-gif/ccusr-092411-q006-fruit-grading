"""证据链:角色约束、环节顺序、封识核验与缺口检测。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fruit_trace import FakeClock, Stage, TraceSystem, testing
from fruit_trace.errors import DomainError, EvidenceGapError, PermissionDeniedError


class EvidenceChainTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.system = TraceSystem(clock=self.clock)
        self.ids = testing.seed_base(self.system)
        self.lot_id = self.ids["lot_id"]

    def tearDown(self):
        self.system.close()

    def test_full_chain_verifies_ok(self):
        testing.run_evidence_chain(self.system, self.ids)
        report = self.system.evidence.verify_chain(self.lot_id)
        self.assertTrue(report.ok, msg=str(report))
        self.assertEqual([e["stage"] for e in report.events], [
            "packing", "sealing", "port_handover", "market_sampling",
        ])
        # 链式引用:每个环节指向前一环节
        ids = [e["id"] for e in report.events]
        prevs = [e["prev_event_id"] for e in report.events]
        self.assertIsNone(prevs[0])
        self.assertEqual(prevs[1:], ids[:-1])

    def test_role_cannot_sign_other_stage(self):
        # 封识员不能签署装箱环节
        with self.assertRaises(PermissionDeniedError):
            self.system.evidence.record_event(
                self.lot_id, Stage.PACKING, self.ids["sealer"], testing.packing_payload(self.ids)
            )
        # 审计角色不能签署任何环节
        with self.assertRaises(PermissionDeniedError):
            self.system.evidence.record_event(
                self.lot_id, Stage.PACKING, self.ids["auditor"], testing.packing_payload(self.ids)
            )

    def test_stage_requires_previous_stage(self):
        # 尚未装箱,不能直接封识
        with self.assertRaises(EvidenceGapError):
            self.system.evidence.record_event(
                self.lot_id, Stage.SEALING, self.ids["sealer"], testing.sealing_payload(self.ids)
            )

    def test_duplicate_stage_rejected(self):
        self.system.evidence.record_event(
            self.lot_id, Stage.PACKING, self.ids["packer"], testing.packing_payload(self.ids)
        )
        with self.assertRaises(DomainError):
            self.system.evidence.record_event(
                self.lot_id, Stage.PACKING, self.ids["packer"], testing.packing_payload(self.ids)
            )

    def test_seal_mismatch_detected(self):
        testing.run_evidence_chain(self.system, self.ids, tamper_seal=True)
        report = self.system.evidence.verify_chain(self.lot_id)
        self.assertFalse(report.ok)
        self.assertTrue(any("封识号不符" in m for m in report.seal_mismatches))

    def test_broken_seal_detected(self):
        testing.run_evidence_chain(self.system, self.ids, intact=False)
        report = self.system.evidence.verify_chain(self.lot_id)
        self.assertFalse(report.ok)
        self.assertTrue(any("封识已破损" in m for m in report.seal_mismatches))

    def test_missing_stages_are_gaps(self):
        self.system.evidence.record_event(
            self.lot_id, Stage.PACKING, self.ids["packer"], testing.packing_payload(self.ids)
        )
        self.system.evidence.record_event(
            self.lot_id, Stage.SEALING, self.ids["sealer"], testing.sealing_payload(self.ids)
        )
        report = self.system.evidence.verify_chain(self.lot_id)
        self.assertFalse(report.ok)
        self.assertTrue(any("port_handover" in g for g in report.gaps))
        self.assertTrue(any("market_sampling" in g for g in report.gaps))

    def test_packing_list_must_match_manifest(self):
        payload = testing.packing_payload(self.ids)
        payload["containers"][0]["box_count"] = 999
        with self.assertRaises(Exception) as ctx:
            self.system.evidence.record_event(
                self.lot_id, Stage.PACKING, self.ids["packer"], payload
            )
        self.assertIn("不符", str(ctx.exception))

    def test_sample_boxes_must_belong_to_lot(self):
        testing.run_evidence_chain(self.system, self.ids)
        # 市场抽检已签署,换个批次验证:直接构造含外来箱号的抽样证据会失败
        lot2 = self.system.registry.register_lot(
            "LOT-2026-0002", "车厘子", "2026年春季", {"CNTR-09": 4}
        )
        self.system.evidence.record_event(
            lot2["id"], Stage.PACKING, self.ids["packer"],
            {"origin": "智利", "containers": [{"container_no": "CNTR-09", "box_count": 4}]},
        )
        self.system.evidence.record_event(
            lot2["id"], Stage.SEALING, self.ids["sealer"],
            {"seals": [{"container_no": "CNTR-09", "seal_no": "S-9"}]},
        )
        self.system.evidence.record_event(
            lot2["id"], Stage.PORT_HANDOVER, self.ids["receiver"],
            {"port": "上海", "checks": [{"container_no": "CNTR-09", "seal_no": "S-9", "intact": True}]},
        )
        with self.assertRaises(Exception) as ctx:
            self.system.evidence.record_event(
                lot2["id"], Stage.MARKET_SAMPLING, self.ids["inspector"],
                {"location": "市场", "sample_box_ids": [self.ids["box_nos"][0]]},  # 属于第一批
            )
        self.assertIn("不属于本批次", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
