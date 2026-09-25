"""放行裁决。

同一批次同一轮次只接受一次有效裁决:多个检验员同时提交时,
由 (lot_id, round) 唯一约束保证只有第一个事务提交成功,其余被驳回。
幂等键保证客户端重试不会产生第二条裁决。
"""
from __future__ import annotations

import sqlite3

from .. import db
from ..constants import Role, RulingOutcome
from ..errors import (
    NotFoundError,
    PermissionDeniedError,
    RulingConflictError,
    ValidationError,
)


class RulingService:
    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock

    def submit(
        self,
        lot_id: int,
        round_no: int,
        outcome,
        decided_by: int,
        idempotency_key: str,
        note: str = "",
    ) -> dict:
        outcome = RulingOutcome(outcome)
        if round_no < 1:
            raise ValidationError("裁决轮次必须 >= 1")
        if not idempotency_key:
            raise ValidationError("裁决必须携带幂等键")
        with db.tx(self.conn):
            lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (lot_id,))
            if lot is None:
                raise NotFoundError(f"批次不存在: {lot_id}")
            decider = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (decided_by,))
            if decider is None:
                raise NotFoundError(f"用户不存在: {decided_by}")
            if decider["role"] != Role.QA_MANAGER.value:
                raise PermissionDeniedError(
                    f"放行裁决只能由 {Role.QA_MANAGER.value} 作出,"
                    f"{decider['name']} 的角色是 {decider['role']}"
                )
            existing = db.one(
                self.conn,
                "SELECT * FROM rulings WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing is not None:
                if existing["lot_id"] != lot_id:
                    raise ValidationError("幂等键已被其他批次使用")
                return existing  # 幂等重试:返回已存在的裁决
            try:
                ruling_id = db.insert(
                    self.conn,
                    "INSERT INTO rulings(lot_id, round, outcome, decided_by, decided_at,"
                    " idempotency_key, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        lot_id,
                        round_no,
                        outcome.value,
                        decided_by,
                        db.now_iso(self.clock),
                        idempotency_key,
                        note,
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发或重复提交:若是同一幂等键则返回既有裁决,否则本轮已有有效裁决
                existing = db.one(
                    self.conn,
                    "SELECT * FROM rulings WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                if existing is not None and existing["lot_id"] == lot_id:
                    return existing
                raise RulingConflictError(
                    f"批次 {lot['code']} 第 {round_no} 轮已存在有效裁决,本次提交被驳回"
                ) from None
        return self.get(ruling_id)

    def get(self, ruling_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM rulings WHERE id = ?", (ruling_id,))
        if row is None:
            raise NotFoundError(f"裁决不存在: {ruling_id}")
        return row

    def list_for_lot(self, lot_id: int) -> list[dict]:
        return db.all_rows(
            self.conn,
            "SELECT * FROM rulings WHERE lot_id = ? ORDER BY round",
            (lot_id,),
        )
