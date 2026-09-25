"""客户订单登记与状态流转,以及面向客户的通知。"""
from __future__ import annotations

from .. import db
from ..constants import GRADE_RANK, Grade, NotificationKind, OrderStatus
from ..errors import NotFoundError, ValidationError


class OrderService:
    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock

    def create_order(
        self, lot_id: int, customer_name: str, quantity: int, contract_id: int | None = None
    ) -> dict:
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError("订单箱数必须为正整数")
        with db.tx(self.conn):
            lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (lot_id,))
            if lot is None:
                raise NotFoundError(f"批次不存在: {lot_id}")
            order_id = db.insert(
                self.conn,
                "INSERT INTO orders(lot_id, customer_name, contract_id, quantity, status,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lot_id,
                    customer_name,
                    contract_id,
                    quantity,
                    OrderStatus.PENDING.value,
                    db.now_iso(self.clock),
                ),
            )
        return self.get(order_id)

    def get(self, order_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        return row

    def list_for_lot(self, lot_id: int) -> list[dict]:
        return db.all_rows(
            self.conn, "SELECT * FROM orders WHERE lot_id = ? ORDER BY id", (lot_id,)
        )

    # ---------- 供其他服务在事务内调用的内部操作 ----------

    def _mark_affected_locked(self, lot: dict, new_version: int, old_grade: Grade, new_grade: Grade) -> list[dict]:
        """结论降级时,仅标记仍未交付(pending)的订单为受影响并通知客户。

        已交付订单不在此标记(必要时通过召回流程处理)。
        """
        if GRADE_RANK[new_grade] >= GRADE_RANK[old_grade]:
            return []
        pending = db.all_rows(
            self.conn,
            "SELECT * FROM orders WHERE lot_id = ? AND status = ?",
            (lot["id"], OrderStatus.PENDING.value),
        )
        for order in pending:
            self.conn.execute(
                "UPDATE orders SET status = ?, affected_by_version = ? WHERE id = ?",
                (OrderStatus.AFFECTED.value, new_version, order["id"]),
            )
            self._notify_locked(
                lot["id"],
                order["id"],
                order["customer_name"],
                NotificationKind.ORDER_AFFECTED,
                f"批次{lot['code']}等级结论由{old_grade.value}变更为{new_grade.value}"
                f"(第{new_version}版),订单#{order['id']}({order['quantity']}箱)暂停交付,"
                f"等待质量复核",
            )
        return pending

    def _notify_locked(
        self,
        lot_id: int,
        order_id: int | None,
        customer_name: str,
        kind: NotificationKind,
        message: str,
    ) -> dict:
        notification_id = db.insert(
            self.conn,
            "INSERT INTO notifications(lot_id, order_id, customer_name, kind, message,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (lot_id, order_id, customer_name, kind.value, message, db.now_iso(self.clock)),
        )
        return db.one(self.conn, "SELECT * FROM notifications WHERE id = ?", (notification_id,))
