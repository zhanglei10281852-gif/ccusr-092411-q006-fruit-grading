"""端到端演示:进口车厘子批次的分级追溯全流程。

场景:商超客户提出等级索赔,质量团队借助本系统还原证据链并处置。

  1. 主数据:角色、合同、三级抽样规则、批次箱单
  2. 证据链:产地装箱 → 集装箱封识 → 到港交接 → 市场抽检
  3. 初检形成等级结论 v1;登记客户订单
  4. 裁决 + 有条件放行(限量、限期);交付与限量控制
  5. 样本补录:保留当时结论
  6. 客户复检推翻初检:产生新版本,仅标记未交付订单,自动召回
  7. 并发裁决:两名检验员同时提交,只形成一次有效裁决
  8. 审计:从任意箱号出发还原完整证据
  9. 反例:封识不符 / 证据缺口 → 直接阻断放行

运行: python3 examples/demo.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fruit_trace import FakeClock, Stage, TraceSystem, testing
from fruit_trace.errors import ReleaseBlockedError, RulingConflictError, ValidationError
from fruit_trace.services.audit import format_audit_report

T0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> None:
    clock = FakeClock(T0)
    system = TraceSystem(clock=clock)

    section("1. 主数据:角色 / 合同 / 抽样规则 / 批次箱单")
    ids = testing.seed_base(system)
    lot = system.registry.get_lot(ids["lot_id"])
    print(f"批次 {lot['code']}: {lot['variety']} / {lot['season']},"
          f" {len(ids['containers'])} 柜共 {len(ids['box_nos'])} 箱, 状态={lot['state']}")
    rule = system.sampling.select_rule(lot["variety"], lot["season"], lot["contract_id"])
    print(f"按品种+产季+客户合同选用抽样规则: 「{rule['name']}」"
          f"(比例{rule['sample_ratio']:.0%}, 阈值{rule['grade_thresholds']})")
    size = system.sampling.sample_size(rule, len(ids["box_nos"]))
    print(f"本批 {len(ids['box_nos'])} 箱 → 应抽 {size} 箱")

    section("2. 证据链:装箱 → 封识 → 交接 → 抽检(各角色签署各自环节)")
    testing.run_evidence_chain(system, ids)
    chain = system.evidence.verify_chain(ids["lot_id"])
    for event in chain.events:
        print(f"  [{event['stage']}] 发生={event['occurred_at']} 签名={event['signature'][:16]}…")
    print(f"证据链核验: {'通过' if chain.ok else '存在问题'}")

    section("3. 市场初检 → 等级结论 v1")
    clock.advance(hours=2)
    inspection = testing.run_initial_inspection(system, ids, total=1000, defects=5)
    print(f"初检缺陷率 {inspection['defect_rate']:.2%} → 判定 {inspection['grade']}"
          f"(结论第 {inspection['conclusion_version']} 版)")

    section("4. 裁决 + 有条件放行(限量 400 箱, 有效期 7 天)→ 交付")
    order1 = system.orders.create_order(ids["lot_id"], "客户甲(鲜果汇)", 300)
    order2 = system.orders.create_order(ids["lot_id"], "客户乙(果然鲜)", 150)
    ruling = system.rulings.submit(ids["lot_id"], 1, "conditional", ids["qa"], "rk-2026-001")
    release = system.releases.request_release(
        ids["lot_id"], ruling["id"], "conditional",
        max_quantity=400, valid_until=clock.now() + timedelta(days=7), actor_id=ids["qa"],
    )
    print(f"放行#{release['id']}: {release['kind']}, 限 {release['max_quantity']} 箱,"
          f" 有效期至 {release['valid_until']}")
    system.releases.deliver_order(order1["id"])
    print(f"订单#{order1['id']}(客户甲 300 箱)已交付")
    try:
        system.releases.deliver_order(order2["id"])
    except ValidationError as exc:
        print(f"订单#{order2['id']}(客户乙 150 箱)交付被拒: {exc}")

    section("5. 样本补录:保留当时结论")
    clock.advance(hours=3)
    late = system.inspections.record_inspection(
        ids["lot_id"], "initial", ids["inspector"], testing.sample_boxes(ids, 4),
        400, 16, occurred_at=T0 + timedelta(hours=1), note="产地留样复测补录",
    )
    current = system.inspections.current_conclusion(ids["lot_id"])
    print(f"补录检验单#{late['id']}(缺陷率 {late['defect_rate']:.2%}, 判定 {late['grade']})"
          f" 归档到 v{late['conclusion_version']}")
    print(f"当前结论仍为 v{current['version']} {current['grade']} —— 当时结论被保留")

    section("6. 客户复检推翻初检 → 新版本 + 标记未交付订单 + 自动召回")
    clock.advance(days=2)
    re = testing.run_reinspection(system, ids, total=1000, defects=120)
    print(f"复检缺陷率 {re['defect_rate']:.2%} → 判定 {re['grade']}")
    for c in system.inspections.conclusions_for_lot(ids["lot_id"]):
        print(f"  v{c['version']} [{c['status']}] {c['grade']} — {c['reason']}")
    print(f"订单#{order1['id']} 状态: {system.orders.get(order1['id'])['status']}(已交付→召回)")
    print(f"订单#{order2['id']} 状态: {system.orders.get(order2['id'])['status']}(未交付→受影响)")
    print(f"放行#{release['id']} 状态: {system.releases.get_release(release['id'])['status']}")

    section("7. 并发裁决:两名检验员同时提交第 2 轮,只形成一次有效裁决")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = f"{tmp}/trace.db"
        setup = TraceSystem(db_path, clock=clock)
        ids2 = testing.seed_base(setup)
        setup.close()
        barrier = threading.Barrier(2)
        outcomes = []

        def submit(i):
            s = TraceSystem(db_path, clock=clock)
            try:
                barrier.wait()
                r = s.rulings.submit(ids2["lot_id"], 2, "pass", ids2["qa"], f"rk-race-{i}")
                outcomes.append(("接受", r["idempotency_key"]))
            except RulingConflictError:
                outcomes.append(("驳回", f"rk-race-{i}"))
            finally:
                s.close()

        threads = [threading.Thread(target=submit, args=(i,)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for status, key in sorted(outcomes):
            print(f"  提交 {key}: {status}")

    section("8. 审计:从箱号出发还原完整证据")
    box_no = ids["box_nos"][6]
    print(format_audit_report(system.audit.audit_box(box_no)))

    section("9. 反例:封识不符 / 证据缺口 → 直接阻断放行")
    # 批次二:到港核验发现封识号与产地记录不符
    lot3 = system.registry.register_lot(
        "LOT-2026-0002", "车厘子", "2026年春季", {"CNTR-88": 10},
        contract_id=ids["contract_id"],
    )
    boxes3 = [b["box_no"] for b in system.registry.lot_boxes(lot3["id"])]
    ids3 = {**ids, "lot_id": lot3["id"], "lot_code": lot3["code"],
            "containers": {"CNTR-88": boxes3}, "box_nos": boxes3}
    testing.run_evidence_chain(system, ids3, tamper_seal=True)
    testing.run_initial_inspection(system, ids3)
    ruling3 = system.rulings.submit(lot3["id"], 1, "conditional", ids["qa"], "rk-2026-002")
    try:
        system.releases.request_release(
            lot3["id"], ruling3["id"], "conditional",
            max_quantity=50, valid_until=clock.now() + timedelta(days=7), actor_id=ids["qa"],
        )
    except ReleaseBlockedError as exc:
        print(f"批次 {lot3['code']} 放行被阻断:")
        for reason in exc.reasons:
            print(f"  ✗ {reason}")

    # 批次三:只有产地装箱与封识,缺少到港交接与市场抽检(证据缺口)
    lot4 = system.registry.register_lot(
        "LOT-2026-0003", "车厘子", "2026年春季", {"CNTR-99": 10},
        contract_id=ids["contract_id"],
    )
    boxes4 = [b["box_no"] for b in system.registry.lot_boxes(lot4["id"])]
    ids4 = {**ids, "lot_id": lot4["id"], "lot_code": lot4["code"],
            "containers": {"CNTR-99": boxes4}, "box_nos": boxes4}
    system.evidence.record_event(lot4["id"], Stage.PACKING, ids["packer"], testing.packing_payload(ids4))
    system.evidence.record_event(lot4["id"], Stage.SEALING, ids["sealer"], testing.sealing_payload(ids4))
    ruling4 = system.rulings.submit(lot4["id"], 1, "conditional", ids["qa"], "rk-2026-003")
    try:
        system.releases.request_release(
            lot4["id"], ruling4["id"], "conditional",
            max_quantity=50, valid_until=clock.now() + timedelta(days=7), actor_id=ids["qa"],
        )
    except ReleaseBlockedError as exc:
        print(f"批次 {lot4['code']} 放行被阻断:")
        for reason in exc.reasons:
            print(f"  ✗ {reason}")

    system.close()
    print("\n演示结束。")


if __name__ == "__main__":
    main()
