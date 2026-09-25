"""批次状态机:状态只能沿 LOT_STATE_ORDER 前进,不能回退。"""
from __future__ import annotations

import sqlite3

from . import db
from .constants import LOT_STATE_ORDER, LotState
from .errors import NotFoundError


def advance_lot_state(conn: sqlite3.Connection, lot_id: int, target: LotState) -> None:
    """将批次状态推进到 target;若当前状态已不早于 target 则保持不变。"""
    lot = db.one(conn, "SELECT id, state FROM lots WHERE id = ?", (lot_id,))
    if lot is None:
        raise NotFoundError(f"批次不存在: {lot_id}")
    current = LotState(lot["state"])
    if LOT_STATE_ORDER.index(target) > LOT_STATE_ORDER.index(current):
        conn.execute("UPDATE lots SET state = ? WHERE id = ?", (target.value, lot_id))
