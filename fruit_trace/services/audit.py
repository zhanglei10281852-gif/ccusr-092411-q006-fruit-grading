"""审计:从任意箱号出发,还原等级为何成立、结论怎样变化、哪些客户已收到通知。"""
from __future__ import annotations

from .. import db
from ..errors import NotFoundError


class AuditService:
    def __init__(self, conn, evidence):
        self.conn = conn
        self.evidence = evidence

    def audit_box(self, box_no: str) -> dict:
        box = db.one(self.conn, "SELECT * FROM boxes WHERE box_no = ?", (box_no,))
        if box is None:
            raise NotFoundError(f"箱号不存在: {box_no}")
        lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (box["lot_id"],))
        container = db.one(
            self.conn, "SELECT * FROM containers WHERE id = ?", (box["container_id"],)
        )
        lot_id = lot["id"]

        chain = self.evidence.verify_chain(lot_id)
        events = []
        for event in chain.events:
            actor = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (event["actor_id"],))
            events.append(
                {
                    **event,
                    "actor_name": actor["name"] if actor else None,
                    "actor_role": actor["role"] if actor else None,
                }
            )

        conclusions = db.all_rows(
            self.conn,
            "SELECT * FROM grade_conclusions WHERE lot_id = ? ORDER BY version",
            (lot_id,),
        )
        for conclusion in conclusions:
            conclusion["basis"] = db.loads(conclusion["basis"])
        current = next((c for c in conclusions if c["status"] == "current"), None)

        inspections = db.all_rows(
            self.conn, "SELECT * FROM inspections WHERE lot_id = ? ORDER BY id", (lot_id,)
        )
        for inspection in inspections:
            inspection["sample_box_ids"] = db.loads(inspection["sample_box_ids"])
            inspector = db.one(
                self.conn, "SELECT * FROM users WHERE id = ?", (inspection["inspector_id"],)
            )
            inspection["inspector_name"] = inspector["name"] if inspector else None

        orders = db.all_rows(
            self.conn, "SELECT * FROM orders WHERE lot_id = ? ORDER BY id", (lot_id,)
        )
        notifications = db.all_rows(
            self.conn,
            "SELECT * FROM notifications WHERE lot_id = ? ORDER BY id",
            (lot_id,),
        )
        releases = db.all_rows(
            self.conn, "SELECT * FROM releases WHERE lot_id = ? ORDER BY id", (lot_id,)
        )
        for release in releases:
            release["recalls"] = db.all_rows(
                self.conn,
                "SELECT * FROM recalls WHERE release_id = ? ORDER BY id",
                (release["id"],),
            )

        return {
            "box": box,
            "container": container,
            "lot": lot,
            "evidence_chain": {
                "ok": chain.ok,
                "gaps": chain.gaps,
                "seal_mismatches": chain.seal_mismatches,
                "violations": chain.violations,
                "events": events,
            },
            "current_conclusion": current,
            "conclusion_history": conclusions,
            "inspections": inspections,
            "orders": orders,
            "notifications": notifications,
            "releases": releases,
        }


def format_audit_report(report: dict) -> str:
    """将审计报告渲染为可读文本。"""
    lines = []
    box = report["box"]
    lot = report["lot"]
    container = report["container"]
    lines.append(f"箱号: {box['box_no']}")
    lines.append(
        f"批次: {lot['code']}(品种={lot['variety']},产季={lot['season']},状态={lot['state']})"
    )
    lines.append(f"集装箱: {container['container_no']}")

    chain = report["evidence_chain"]
    lines.append("")
    lines.append(f"证据链: {'完整有效' if chain['ok'] else '存在问题'}")
    for event in chain["events"]:
        lines.append(
            f"  [{event['stage']}] 签署人={event['actor_name']}({event['actor_role']})"
            f" 发生={event['occurred_at']} 录入={event['recorded_at']}"
            f" 签名={event['signature'][:12]}…"
        )
    for problem in chain["gaps"] + chain["seal_mismatches"] + chain["violations"]:
        lines.append(f"  ! {problem}")

    current = report["current_conclusion"]
    lines.append("")
    if current is None:
        lines.append("当前等级结论: 无")
    else:
        basis = current["basis"]
        lines.append(
            f"当前等级结论: {current['grade']}(第{current['version']}版,{current['created_at']})"
        )
        lines.append(
            f"  依据: 规则「{basis['rule_name']}」,缺陷率 {basis['defect_rate']:.2%},"
            f"阈值 {basis['grade_thresholds']}"
        )
        lines.append(f"  样本箱: {', '.join(basis['sample_box_ids'])}")
        lines.append(f"  成立原因: {current['reason']}")

    lines.append("")
    lines.append("结论变迁:")
    for conclusion in report["conclusion_history"]:
        lines.append(
            f"  v{conclusion['version']} [{conclusion['status']}] {conclusion['grade']}"
            f" @ {conclusion['created_at']} — {conclusion['reason']}"
        )

    lines.append("")
    lines.append("检验记录:")
    for inspection in report["inspections"]:
        backfilled = "(补录)" if inspection["backfilled"] else ""
        lines.append(
            f"  #{inspection['id']} {inspection['kind']}{backfilled}"
            f" 检验员={inspection['inspector_name']}"
            f" 缺陷率={inspection['defect_rate']:.2%} 判定={inspection['grade']}"
            f" 归属版本=v{inspection['conclusion_version']}"
        )

    lines.append("")
    lines.append("订单:")
    for order in report["orders"]:
        lines.append(
            f"  #{order['id']} 客户={order['customer_name']} {order['quantity']}箱"
            f" 状态={order['status']}"
        )

    lines.append("")
    lines.append("客户通知:")
    notified = report["notifications"]
    if not notified:
        lines.append("  (无)")
    for notification in notified:
        lines.append(
            f"  → {notification['customer_name']} [{notification['kind']}]"
            f" @ {notification['created_at']}: {notification['message']}"
        )

    lines.append("")
    lines.append("放行与召回:")
    if not report["releases"]:
        lines.append("  (无)")
    for release in report["releases"]:
        lines.append(
            f"  放行#{release['id']} {release['kind']} 状态={release['status']}"
            f" 限量={release['max_quantity']} 有效期至={release['valid_until']}"
        )
        for recall in release["recalls"]:
            lines.append(f"    召回 @ {recall['created_at']}: {recall['reason']}")
    return "\n".join(lines)
