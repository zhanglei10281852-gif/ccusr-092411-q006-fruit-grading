"""放行、交付、到期与召回。

放行前置条件(任一不满足即阻断,返回全部原因):
- 证据链完整:装箱、封识、到港交接、市场抽检各环节齐全且链式引用正确;
- 封识一致:到港核验的封识号与产地封识一致且完好;
- 当前等级结论非不合格;
- 裁决结果允许(有条件裁决只能申请有条件放行)。

有条件放行必须限定数量(max_quantity)与有效期(valid_until);
交付累计不得超过限量;期满或后续检验不合格均可召回。
"""
from __future__ import annotations

import sqlite3

from .. import db
from ..constants import (
    Grade,
    LotState,
    NotificationKind,
    OrderStatus,
    ReleaseKind,
    ReleaseStatus,
    Role,
    RulingOutcome,
)
from ..errors import (
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ReleaseBlockedError,
    ValidationError,
)
from ..lots import advance_lot_state


class ReleaseService:
    def __init__(self, conn, clock, evidence, orders):
        self.conn = conn
        self.clock = clock
        self.evidence = evidence
        self.orders = orders

    # ---------- 放行 ----------
    def request_release(
        self,
        lot_id: int,
        ruling_id: int,
        kind,
        max_quantity: int | None = None,
        valid_until=None,
        actor_id: int | None = None,
    ) -> dict:
        kind = ReleaseKind(kind)
        with db.tx(self.conn):
            lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (lot_id,))
            if lot is None:
                raise NotFoundError(f"批次不存在: {lot_id}")
            self._require_qa(actor_id)
            ruling = db.one(self.conn, "SELECT * FROM rulings WHERE id = ?", (ruling_id,))
            if ruling is None:
                raise NotFoundError(f"裁决不存在: {ruling_id}")
            if ruling["lot_id"] != lot_id:
                raise ValidationError("裁决不属于该批次")

            chain = self.evidence.verify_chain(lot_id)
            reasons = [*chain.gaps, *chain.seal_mismatches, *chain.violations]
            current = db.one(
                self.conn,
                "SELECT * FROM grade_conclusions WHERE lot_id = ? AND status = 'current'",
                (lot_id,),
            )
            if current is None:
                reasons.append("缺少等级结论,无法放行")
            elif current["grade"] == Grade.REJECT.value:
                reasons.append("当前等级结论为不合格,禁止放行")
            if ruling["outcome"] == RulingOutcome.REJECT.value:
                reasons.append("裁决结果为不予放行")
            elif ruling["outcome"] == RulingOutcome.CONDITIONAL.value and kind is ReleaseKind.FULL:
                reasons.append("裁决为有条件放行,不能申请完全放行")
            if reasons:
                raise ReleaseBlockedError(reasons)

            if kind is ReleaseKind.CONDITIONAL:
                if max_quantity is None or max_quantity <= 0:
                    raise ValidationError("有条件放行必须限定数量(max_quantity > 0)")
                if valid_until is None:
                    raise ValidationError("有条件放行必须限定有效期(valid_until)")
                if db.to_iso(valid_until) <= db.now_iso(self.clock):
                    raise ValidationError("有条件放行的有效期必须晚于当前时间")
            try:
                release_id = db.insert(
                    self.conn,
                    "INSERT INTO releases(lot_id, ruling_id, kind, max_quantity, valid_until,"
                    " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        lot_id,
                        ruling_id,
                        kind.value,
                        max_quantity,
                        db.to_iso(valid_until) if valid_until is not None else None,
                        ReleaseStatus.ACTIVE.value,
                        db.now_iso(self.clock),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("该批次已存在有效放行,请先处理(召回或等待期满)") from None
            advance_lot_state(
                self.conn,
                lot_id,
                LotState.CONDITIONAL if kind is ReleaseKind.CONDITIONAL else LotState.RELEASED,
            )
            self._notify_release_locked(lot, release_id, kind, max_quantity, valid_until)
        return self.get_release(release_id)

    def _notify_release_locked(self, lot, release_id, kind, max_quantity, valid_until) -> None:
        pending = db.all_rows(
            self.conn,
            "SELECT * FROM orders WHERE lot_id = ? AND status = ?",
            (lot["id"], OrderStatus.PENDING.value),
        )
        if kind is ReleaseKind.CONDITIONAL:
            suffix = f"(限{max_quantity}箱,有效期至{db.to_iso(valid_until)})"
        else:
            suffix = "(完全放行)"
        for order in pending:
            self.orders._notify_locked(
                lot["id"],
                order["id"],
                order["customer_name"],
                NotificationKind.RELEASE_GRANTED,
                f"批次{lot['code']}已放行{suffix},订单#{order['id']}"
                f"({order['quantity']}箱)可安排交付",
            )

    # ---------- 交付 ----------
    def deliver_order(self, order_id: int) -> dict:
        """交付订单:批次须有有效放行,有条件放行下累计交付不得超过限量。"""
        # 先结算可能已过期的放行(独立事务提交,使状态变更生效)
        order = self.orders.get(order_id)
        release = self._active_release(order["lot_id"])
        if release is not None and self._is_expired(release):
            with db.tx(self.conn):
                self._expire_locked(release)
            raise ReleaseBlockedError(
                [f"放行#{release['id']}已过有效期 {release['valid_until']},禁止交付"]
            )
        with db.tx(self.conn):
            order = self.orders.get(order_id)
            if order["status"] != OrderStatus.PENDING.value:
                raise ValidationError(
                    f"订单#{order_id}状态为 {order['status']},不可交付"
                    f"(受影响订单需质量经理复核后重新放行)"
                )
            release = self._active_release(order["lot_id"])
            if release is None:
                raise ReleaseBlockedError(["批次无有效放行,禁止交付"])
            if self._is_expired(release):
                raise ReleaseBlockedError(
                    [f"放行#{release['id']}已过有效期 {release['valid_until']},禁止交付"]
                )
            if release["kind"] == ReleaseKind.CONDITIONAL.value:
                used = db.one(
                    self.conn,
                    "SELECT COALESCE(SUM(quantity), 0) AS q FROM orders"
                    " WHERE release_id = ? AND status = ?",
                    (release["id"], OrderStatus.DELIVERED.value),
                )["q"]
                if used + order["quantity"] > release["max_quantity"]:
                    raise ValidationError(
                        f"超出有条件放行数量限制:已交付{used}箱 + 本单{order['quantity']}箱"
                        f" > 限{release['max_quantity']}箱"
                    )
            self.conn.execute(
                "UPDATE orders SET status = ?, release_id = ?, delivered_at = ? WHERE id = ?",
                (
                    OrderStatus.DELIVERED.value,
                    release["id"],
                    db.now_iso(self.clock),
                    order_id,
                ),
            )
        return self.orders.get(order_id)

    # ---------- 到期 ----------
    def expire_overdue(self) -> list[dict]:
        """将已过有效期的有效放行标记为 EXPIRED(期满后可通过 recall 召回)。"""
        expired = []
        with db.tx(self.conn):
            rows = db.all_rows(
                self.conn,
                "SELECT * FROM releases WHERE status = ? AND valid_until IS NOT NULL",
                (ReleaseStatus.ACTIVE.value,),
            )
            for release in rows:
                if db.parse_iso(release["valid_until"]) <= self.clock.now():
                    self._expire_locked(release)
                    expired.append(self.get_release(release["id"]))
        return expired

    def _is_expired(self, release: dict) -> bool:
        return (
            release["valid_until"] is not None
            and db.parse_iso(release["valid_until"]) <= self.clock.now()
        )

    def _expire_locked(self, release: dict) -> None:
        self.conn.execute(
            "UPDATE releases SET status = ? WHERE id = ?",
            (ReleaseStatus.EXPIRED.value, release["id"]),
        )

    # ---------- 召回 ----------
    def recall(self, release_id: int, reason: str, actor_id: int) -> dict:
        """召回放行:已交付订单转为召回并通知客户,未交付订单标记为受影响。"""
        with db.tx(self.conn):
            self._require_qa(actor_id)
            release = db.one(self.conn, "SELECT * FROM releases WHERE id = ?", (release_id,))
            if release is None:
                raise NotFoundError(f"放行不存在: {release_id}")
            if release["status"] not in (ReleaseStatus.ACTIVE.value, ReleaseStatus.EXPIRED.value):
                raise ValidationError(f"放行状态为 {release['status']},不可召回")
            return self._recall_locked(release, reason)

    def _auto_recall_locked(self, lot_id: int, reason: str) -> dict | None:
        """后续检验不合格时由系统触发的自动召回(事务内调用)。"""
        release = self._active_release(lot_id)
        if release is None:
            return None
        return self._recall_locked(release, reason)

    def _recall_locked(self, release: dict, reason: str) -> dict:
        lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (release["lot_id"],))
        self.conn.execute(
            "UPDATE releases SET status = ? WHERE id = ?",
            (ReleaseStatus.RECALLED.value, release["id"]),
        )
        recall_id = db.insert(
            self.conn,
            "INSERT INTO recalls(release_id, reason, created_at) VALUES (?, ?, ?)",
            (release["id"], reason, db.now_iso(self.clock)),
        )
        delivered = db.all_rows(
            self.conn,
            "SELECT * FROM orders WHERE release_id = ? AND status = ?",
            (release["id"], OrderStatus.DELIVERED.value),
        )
        for order in delivered:
            self.conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (OrderStatus.RECALLED.value, order["id"]),
            )
            self.orders._notify_locked(
                lot["id"],
                order["id"],
                order["customer_name"],
                NotificationKind.RECALL,
                f"批次{lot['code']}放行已召回:{reason};订单#{order['id']}已交付的"
                f"{order['quantity']}箱请停止销售并等待退回安排",
            )
        pending = db.all_rows(
            self.conn,
            "SELECT * FROM orders WHERE lot_id = ? AND status = ?",
            (lot["id"], OrderStatus.PENDING.value),
        )
        for order in pending:
            self.conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (OrderStatus.AFFECTED.value, order["id"]),
            )
            self.orders._notify_locked(
                lot["id"],
                order["id"],
                order["customer_name"],
                NotificationKind.ORDER_AFFECTED,
                f"批次{lot['code']}放行被召回:{reason};订单#{order['id']}暂停交付",
            )
        advance_lot_state(self.conn, lot["id"], LotState.RECALLED)
        return db.one(self.conn, "SELECT * FROM recalls WHERE id = ?", (recall_id,))

    # ---------- 查询 ----------
    def get_release(self, release_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM releases WHERE id = ?", (release_id,))
        if row is None:
            raise NotFoundError(f"放行不存在: {release_id}")
        return row

    def _active_release(self, lot_id: int) -> dict | None:
        return db.one(
            self.conn,
            "SELECT * FROM releases WHERE lot_id = ? AND status = ?",
            (lot_id, ReleaseStatus.ACTIVE.value),
        )

    def _require_qa(self, actor_id: int | None) -> None:
        if actor_id is None:
            raise ValidationError("该操作必须指定操作人")
        actor = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (actor_id,))
        if actor is None:
            raise NotFoundError(f"用户不存在: {actor_id}")
        if actor["role"] != Role.QA_MANAGER.value:
            raise PermissionDeniedError(
                f"该操作只能由 {Role.QA_MANAGER.value} 执行,{actor['name']} 的角色是 {actor['role']}"
            )
