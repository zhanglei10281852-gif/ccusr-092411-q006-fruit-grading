"""审计：从任意箱号出发还原完整证据故事。

输出：箱属于哪一批 → 五环节证据链 → 等级为何成立（用了哪版规则、
谁在何时签署、是否补录）→ 结论怎样逐版变化 → 哪些客户已收到通知。
"""
from __future__ import annotations

from typing import Any

from .chain import verify_lot
from .roles import ROLE_LABEL
from .state import LotState, replay
from .store import Store

NOTICE_KIND_LABEL = {
    "release": "放行通知",
    "overturn": "复检改判通知",
    "recall": "召回通知",
}


def lot_for_box(store: Store, box_no: str) -> LotState | None:
    lot_no = store.find_box_lot(box_no)
    if lot_no is None:
        return None
    return replay(store.events_for(lot_no))[lot_no]


def build_box_report(store: Store, box_no: str) -> dict[str, Any]:
    lot = lot_for_box(store, box_no)
    if lot is None:
        raise KeyError(f"箱号未在任何批次中找到：{box_no}")

    box = lot.boxes[box_no]
    chain = verify_lot(lot)

    # 等级为何成立 + 结论怎样变化
    grade_versions: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for record in lot.grade_history():
        sample = lot.samples.get(record.round)
        actor_role = ""
        try:
            actor_role = store.user_role(record.event.actor).value
        except KeyError:
            actor_role = record.event.actor
        changed = (
            previous is not None
            and (previous["grade"] != record.grade or previous["verdict"] != record.verdict)
        )
        grade_versions.append({
            "round": record.round,
            "kind": record.kind,
            "grade": record.grade,
            "verdict": record.verdict,
            "defect_rate": record.defect_rate,
            "rule": f"{record.rule_id} v{record.rule_version}",
            "conclusion": record.conclusion,
            "inspector": record.event.actor,
            "inspector_role": actor_role,
            "occurred_at": record.event.occurred_at,
            "recorded_at": record.event.recorded_at,
            "backfilled": record.event.payload.get("backfilled", False),
            "supersedes_round": record.supersedes_round,
            "changed_from_previous": changed,
            "box_included": sample is not None and box_no in sample.box_seq,
        })
        previous = grade_versions[-1]

    # 哪些客户已经收到通知（含订单交付状态）
    customer_notices: list[dict[str, Any]] = []
    for order_no in sorted(lot.orders):
        order = lot.orders[order_no]
        for event in order.notices:
            p = event.payload
            customer_notices.append({
                "order_no": order_no,
                "customer": p.get("customer", order.customer),
                "delivered": order.delivered,
                "delivered_at": order.delivered_at,
                "round": p.get("round"),
                "kind": p.get("kind"),
                "kind_label": NOTICE_KIND_LABEL.get(p.get("kind", ""), p.get("kind", "")),
                "message": p.get("message", ""),
                "notified_at": event.occurred_at,
            })

    return {
        "box_no": box_no,
        "lot_no": lot.lot_no,
        "commodity": lot.commodity,
        "season": lot.season,
        "contract_no": lot.contract_no,
        "declared_grade": box.get("declared_grade", ""),
        "packed_at": box.get("packed_at"),
        "evidence_chain": {
            "clear": chain.clear,
            "seal_ok": chain.seal_ok,
            "gaps": chain.gaps,
            "seal_no": lot.seal_no,
            "handover": lot.handover,
        },
        "current_grade": lot.current_inspection.grade if lot.current_inspection else None,
        "current_verdict": (
            lot.current_inspection.verdict if lot.current_inspection else None
        ),
        "grade_versions": grade_versions,
        "customer_notices": customer_notices,
    }


def render_box_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"箱号审计报告：{report['box_no']}")
    lines.append(
        f"所属批次 {report['lot_no']}（{report['commodity']} / 产季 {report['season']} "
        f"/ 合同 {report['contract_no']}），产地装箱等级：{report['declared_grade']}"
    )
    chain = report["evidence_chain"]
    if chain["clear"]:
        lines.append(f"证据链：完整 ✓（封识 {chain['seal_no']} 到港核对一致）")
    else:
        lines.append("证据链：存在缺口 ✗")
        for gap in chain["gaps"]:
            lines.append(f"  - {gap}")

    lines.append("")
    lines.append("等级结论版本：")
    if not report["grade_versions"]:
        lines.append("  （尚无检验裁决）")
    for v in report["grade_versions"]:
        tag = "补录" if v["backfilled"] else v["kind"]
        mark = " ★改判" if v["changed_from_previous"] else ""
        scope = "本箱在抽样范围内" if v["box_included"] else "本箱未直接抽中（按批推定）"
        lines.append(
            f"  第{v['round']}轮[{tag}] {v['grade']} / {v['verdict']}"
            f"（不合格率 {v['defect_rate']:.2%}，{v['rule']}）{mark}"
        )
        lines.append(
            f"      签署人 {v['inspector']}（{ROLE_LABEL.get(_role_enum(v['inspector_role']), v['inspector_role'])}）"
            f" 业务时间 {v['occurred_at']} 录入时间 {v['recorded_at']}"
        )
        lines.append(f"      结论：{v['conclusion'] or '（无备注）'}；{scope}")

    lines.append("")
    lines.append("客户通知：")
    if not report["customer_notices"]:
        lines.append("  （尚无客户收到通知）")
    for n in report["customer_notices"]:
        status = "已交付" if n["delivered"] else "未交付"
        lines.append(
            f"  订单 {n['order_no']} / 客户 {n['customer']}（{status}）："
            f"{n['kind_label']} - {n['message']}（{n['notified_at']}）"
        )
    return "\n".join(lines)


def _role_enum(value: str):
    from .roles import Role
    try:
        return Role(value)
    except ValueError:
        return None
