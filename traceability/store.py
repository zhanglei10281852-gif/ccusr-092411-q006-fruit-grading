"""只追加事件存储（SQLite）。

硬约束：
- event_id 全局唯一（重复提交幂等返回）；
- 每个批次每个检验轮次最多一条 inspection.submitted（部分唯一索引），
  多个检验员并发提交时由数据库串行化，只可能有一次有效裁决；
- 同一抽样轮次、同一裁决编号、同一订单同一轮次通知同样唯一。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .errors import ImmutableHistoryError
from .events import Event, EVENT_TYPES
from .roles import Role

SCHEMA = """
create table if not exists events (
    seq          integer primary key autoincrement,
    event_id     text not null unique,
    event_type   text not null,
    aggregate_id text not null,
    occurred_at  text not null,
    recorded_at  text not null,
    actor        text not null,
    payload      text not null
);
create index if not exists ix_events_aggregate on events(aggregate_id);
create index if not exists ix_events_type on events(event_type);

-- 一个批次的一轮检验只允许一次有效裁决（初检=1，复检=2…）
create unique index if not exists ux_inspection_round
    on events(aggregate_id, cast(json_extract(payload, '$.round') as integer))
    where event_type = 'inspection.submitted';

-- 一轮只允许一次抽样登记
create unique index if not exists ux_sample_round
    on events(aggregate_id, cast(json_extract(payload, '$.round') as integer))
    where event_type = 'sample.collected';

-- 裁决编号唯一
create unique index if not exists ux_decision_no
    on events(json_extract(payload, '$.decision_no'))
    where event_type = 'decision.issued';

-- 同一轮次、同一通知类型对同一订单只通知一次（放行/改判/召回互不冲突）
create unique index if not exists ux_order_notice
    on events(aggregate_id, json_extract(payload, '$.order_no'),
              cast(json_extract(payload, '$.round') as integer),
              json_extract(payload, '$.kind'))
    where event_type = 'order.notified';

create table if not exists users (
    user_id text primary key,
    name    text not null,
    role    text not null
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma busy_timeout=5000")
        self._conn.execute("pragma journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 用户目录 ----
    def register_user(self, user_id: str, name: str, role: Role) -> None:
        self._conn.execute(
            "insert or replace into users(user_id, name, role) values (?, ?, ?)",
            (user_id, name, role.value),
        )

    def user_role(self, user_id: str) -> Role:
        row = self._conn.execute(
            "select role from users where user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"未知用户：{user_id}")
        return Role(row["role"])

    def user_name(self, user_id: str) -> str:
        row = self._conn.execute(
            "select name from users where user_id = ?", (user_id,)
        ).fetchone()
        return row["name"] if row else user_id

    # ---- 事件写入 ----
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("begin immediate")
        try:
            yield conn
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise

    def append(
        self,
        event: Event,
        check: Callable[[sqlite3.Connection], None] | None = None,
    ) -> Event:
        """在一个写事务内先执行 check（业务前置校验），再追加事件。

        check 抛出异常则整体回滚。若 event_id 已存在，幂等返回旧事件。
        """
        if event.event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{event.event_type}")
        recorded = event.recorded_at or datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            existing = conn.execute(
                "select event_id from events where event_id = ?", (event.event_id,)
            ).fetchone()
            if existing is not None:
                return event  # 幂等：同一事件重复提交
            if check is not None:
                check(conn)
            try:
                conn.execute(
                    "insert into events(event_id, event_type, aggregate_id, occurred_at, "
                    "recorded_at, actor, payload) values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.event_type,
                        event.aggregate_id,
                        event.occurred_at,
                        recorded,
                        event.actor,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc)
                if "ux_inspection_round" in message or "ux_sample_round" in message:
                    raise ImmutableHistoryError(
                        "该轮次已有有效裁决/抽样，重复提交不能形成第二次裁决"
                    ) from exc
                if "ux_decision_no" in message:
                    raise ImmutableHistoryError("裁决编号已存在") from exc
                if "ux_order_notice" in message:
                    raise ImmutableHistoryError("该订单在本轮已通知过") from exc
                raise
        stored = Event(
            event_id=event.event_id,
            event_type=event.event_type,
            aggregate_id=event.aggregate_id,
            occurred_at=event.occurred_at,
            actor=event.actor,
            payload=dict(event.payload),
            recorded_at=recorded,
        )
        return stored

    # ---- 读取 ----
    def events_for(self, aggregate_id: str) -> list[Event]:
        rows = self._conn.execute(
            "select * from events where aggregate_id = ? order by seq", (aggregate_id,)
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def all_events(self) -> list[Event]:
        rows = self._conn.execute("select * from events order by seq").fetchall()
        return [self._row_to_event(row) for row in rows]

    def exists_event(self, conn: sqlite3.Connection, event_id: str) -> bool:
        return conn.execute(
            "select 1 from events where event_id = ?", (event_id,)
        ).fetchone() is not None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            event_type=row["event_type"],
            aggregate_id=row["aggregate_id"],
            occurred_at=row["occurred_at"],
            actor=row["actor"],
            payload=json.loads(row["payload"]),
            recorded_at=row["recorded_at"],
            seq=row["seq"],
        )

    # ---- 查询辅助 ----
    def find_box_lot(self, box_no: str) -> str | None:
        row = self._conn.execute(
            "select aggregate_id from events where event_type = 'box.packed' "
            "and json_extract(payload, '$.box_no') = ?",
            (box_no,),
        ).fetchone()
        return row["aggregate_id"] if row else None

    def notices_for_order(self, order_no: str) -> list[Event]:
        rows = self._conn.execute(
            "select * from events where event_type = 'order.notified' "
            "and json_extract(payload, '$.order_no') = ? order by seq",
            (order_no,),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]
