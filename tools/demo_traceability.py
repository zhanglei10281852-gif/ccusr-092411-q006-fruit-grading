"""端到端演示：用车厘子批次走完全部追溯环节，并从箱号生成审计报告。

运行：python3 tools/demo_traceability.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traceability import (  # noqa: E402
    AcceptanceRule, GradeLevel, QualityTraceService, Role, Store, Verdict,
    build_box_report, render_box_report,
)
from traceability.timeutil import parse  # noqa: E402


def main() -> None:
    store = Store(":memory:")
    svc = QualityTraceService(store)

    # ---- 角色开户 ----
    users = [
        ("u-pack", "产地装箱员张工", Role.ORIGIN_PACKER),
        ("u-seal", "承运人李工", Role.CARRIER_SEALER),
        ("u-port", "到港交接王工", Role.PORT_RECEIVER),
        ("u-market", "市场检验员赵工", Role.MARKET_INSPECTOR),
        ("u-cust", "客户检验员陈工", Role.CUSTOMER_INSPECTOR),
        ("u-qm", "质量经理孙总", Role.QUALITY_MANAGER),
    ]
    for uid, name, role in users:
        svc.register_user(uid, name, role)

    # ---- 抽样规则：通用规则 + 合同专用规则（更严）----
    levels = (
        GradeLevel("A级", 0.05, Verdict.QUALIFIED),
        GradeLevel("B级", 0.10, Verdict.CONDITIONAL),
        GradeLevel("C级", 0.20, Verdict.REJECTED),
    )
    svc.publish_rule("u-qm", AcceptanceRule(
        rule_id="R-GENERAL", version=1, commodity="*", season="*", contract_no="*",
        effective_from=parse("2026-01-01T00:00:00+08:00"), levels=levels,
        min_boxes=2, max_boxes=5, min_units=100,
        published_at=parse("2025-12-01T00:00:00+08:00"), note="进口水果通用AQL"))
    svc.publish_rule("u-qm", AcceptanceRule(
        rule_id="R-CONTRACT-X", version=1, commodity="车厘子", season="2026",
        contract_no="HT-X001", effective_from=parse("2026-06-01T00:00:00+08:00"),
        levels=levels, min_boxes=3, max_boxes=8, min_units=150,
        published_at=parse("2026-05-01T00:00:00+08:00"), note="X客户合同加严"))

    lot = "LOT-CHERRY-20260920"

    # ---- 环节1：产地装箱（产地分级表只作为登记附件，不能单独作证）----
    svc.register_lot("u-pack", lot, "车厘子", "2026", "HT-X001", 4,
                     "2026-09-10T08:00:00+08:00",
                     origin_grade_table="智利产地分级表2026版")
    for i in range(1, 5):
        svc.pack_box("u-pack", lot, f"CL-{i:04d}", "A级",
                     f"2026-09-10T09:{i:02d}:00+08:00")

    # ---- 环节2：集装箱施封 ----
    svc.record_seal("u-seal", lot, "SEAL-CL-7788",
                    "2026-09-11T10:00:00+08:00", note="铅封完好")

    # ---- 环节3：到港交接核对封识 ----
    svc.record_handover("u-port", lot, "SEAL-CL-7788",
                        "2026-09-20T08:30:00+08:00", note="封识号一致、外观完好")

    # ---- 环节4：市场抽检（合同规则：至少3箱150单位）----
    svc.collect_sample("u-market", lot, 1, "initial",
                       ["CL-0001", "CL-0002", "CL-0003"], 150,
                       "2026-09-20T10:00:00+08:00")
    svc.submit_inspection("u-market", lot, 1, "initial", 5, 150,
                          "2026-09-20T11:00:00+08:00",
                          conclusion="初检不合格率3.3%，A级合格")

    # ---- 客户订单：一个先交付，一个未交付 ----
    svc.register_order("u-qm", lot, "PO-1001", "华东商超", 2)
    svc.register_order("u-qm", lot, "PO-1002", "华北商超", 2)
    svc.issue_decision("u-qm", lot, "full", 1, "2026-09-20T12:00:00+08:00",
                       reason="初检合格，证据链完整")
    svc.mark_delivered("u-qm", lot, "PO-1001", 2,
                       "2026-09-20T15:00:00+08:00")

    # ---- 环节5：客户复检推翻初检 → 新版本，只通知未交付订单 ----
    svc.collect_sample("u-cust", lot, 2, "reinspection",
                       ["CL-0001", "CL-0002", "CL-0004"], 150,
                       "2026-09-22T10:00:00+08:00")
    svc.submit_inspection("u-cust", lot, 2, "reinspection", 27, 150,
                          "2026-09-22T11:00:00+08:00",
                          conclusion="软化率18%，降为C级，建议拒收并召回")

    # ---- 审计：从任意箱号出发 ----
    report = build_box_report(store, "CL-0004")
    print(render_box_report(report))
    print()

    # ---- 演示阻断：复检不合格后未交付订单不能再凭旧放行提货 ----
    from traceability import ReleaseBlocked
    try:
        svc.mark_delivered("u-qm", lot, "PO-1002", 2,
                           "2026-09-22T16:00:00+08:00")
    except ReleaseBlocked as exc:
        print("交付拦截：")
        for reason in exc.reasons:
            print(f"  - {reason}")


if __name__ == "__main__":
    main()
