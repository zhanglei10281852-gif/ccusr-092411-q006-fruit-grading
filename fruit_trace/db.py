"""SQLite 连接、建表与事务助手。

约定:
- 全部时间以 ISO 8601(含时区)字符串存储,字典序即时间序;
- JSON 字段(payload、basis、sample_box_ids、grade_thresholds)以文本存储;
- 唯一约束承载关键业务不变量:每环节一次签署、每批次一个当前结论、
  每批次每轮一次有效裁决、每批次一个有效放行。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name  TEXT NOT NULL,
    variety        TEXT NOT NULL,
    season         TEXT NOT NULL,
    grade_required TEXT NOT NULL,
    terms          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sampling_rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    variety          TEXT,                 -- NULL 表示不限品种
    season           TEXT,                 -- NULL 表示不限产季
    contract_id      INTEGER REFERENCES contracts(id),  -- NULL 表示非合同专属
    sample_ratio     REAL NOT NULL,        -- 抽样比例(按箱)
    min_sample       INTEGER NOT NULL,     -- 最少抽样箱数
    max_sample       INTEGER NOT NULL,     -- 最多抽样箱数
    grade_thresholds TEXT NOT NULL,        -- JSON {"A":0.02,"B":0.05,"C":0.10} 缺陷率上限
    priority         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    variety     TEXT NOT NULL,
    season      TEXT NOT NULL,
    contract_id INTEGER REFERENCES contracts(id),
    state       TEXT NOT NULL DEFAULT 'registered',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS containers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id       INTEGER NOT NULL REFERENCES lots(id),
    container_no TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS boxes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id       INTEGER NOT NULL REFERENCES lots(id),
    container_id INTEGER NOT NULL REFERENCES containers(id),
    box_no       TEXT NOT NULL UNIQUE
);

-- 证据链:每批次每环节仅一条,后一环节通过 prev_event_id 引用前一环节
CREATE TABLE IF NOT EXISTS evidence_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id        INTEGER NOT NULL REFERENCES lots(id),
    stage         TEXT NOT NULL,
    actor_id      INTEGER NOT NULL REFERENCES users(id),
    occurred_at   TEXT NOT NULL,           -- 业务发生时间
    recorded_at   TEXT NOT NULL,           -- 系统录入时间(补录时晚于发生时间)
    prev_event_id INTEGER REFERENCES evidence_events(id),
    payload       TEXT NOT NULL DEFAULT '{}',
    signature     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_lot_stage ON evidence_events(lot_id, stage);

CREATE TABLE IF NOT EXISTS inspections (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id             INTEGER NOT NULL REFERENCES lots(id),
    kind               TEXT NOT NULL,      -- initial / reinspection
    inspector_id       INTEGER NOT NULL REFERENCES users(id),
    occurred_at        TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    backfilled         INTEGER NOT NULL DEFAULT 0,  -- 1 表示补录(发生时间早于当前结论)
    sample_box_ids     TEXT NOT NULL,      -- JSON 数组
    total_fruits       INTEGER NOT NULL,
    defect_fruits      INTEGER NOT NULL,
    defect_rate        REAL NOT NULL,
    grade              TEXT NOT NULL,      -- 按抽样规则评定的等级
    rule_id            INTEGER REFERENCES sampling_rules(id),
    conclusion_version INTEGER,            -- 归属的结论版本(补录时指向当时版本)
    note               TEXT NOT NULL DEFAULT ''
);

-- 等级结论版本:发布后不可改,结论变化以新版本追加
CREATE TABLE IF NOT EXISTS grade_conclusions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id     INTEGER NOT NULL REFERENCES lots(id),
    version    INTEGER NOT NULL,
    grade      TEXT NOT NULL,
    basis      TEXT NOT NULL,              -- JSON:等级为何成立(规则、缺陷率、样本、检验单)
    reason     TEXT NOT NULL,              -- 产生该版本的原因
    status     TEXT NOT NULL DEFAULT 'current',  -- current / superseded
    created_at TEXT NOT NULL,
    UNIQUE (lot_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_current_conclusion
    ON grade_conclusions(lot_id) WHERE status = 'current';

CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id       INTEGER NOT NULL REFERENCES lots(id),
    ruling_id    INTEGER NOT NULL REFERENCES rulings(id),
    kind         TEXT NOT NULL,            -- full / conditional
    max_quantity INTEGER,                  -- 有条件放行必填
    valid_until  TEXT,                     -- 有条件放行必填
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_release ON releases(lot_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id              INTEGER NOT NULL REFERENCES lots(id),
    customer_name       TEXT NOT NULL,
    contract_id         INTEGER REFERENCES contracts(id),
    quantity            INTEGER NOT NULL,  -- 箱数
    status              TEXT NOT NULL DEFAULT 'pending',
    release_id          INTEGER REFERENCES releases(id),
    affected_by_version INTEGER,           -- 被哪个结论版本标记为受影响
    created_at          TEXT NOT NULL,
    delivered_at        TEXT
);

-- 放行裁决:同一批次同一轮次只接受一次有效裁决;幂等键保证重试安全
CREATE TABLE IF NOT EXISTS rulings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id          INTEGER NOT NULL REFERENCES lots(id),
    round           INTEGER NOT NULL,
    outcome         TEXT NOT NULL,         -- pass / conditional / reject
    decided_by      INTEGER NOT NULL REFERENCES users(id),
    decided_at      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE (lot_id, round),
    UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS recalls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL REFERENCES releases(id),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id        INTEGER NOT NULL REFERENCES lots(id),
    order_id      INTEGER REFERENCES orders(id),
    customer_name TEXT NOT NULL,
    kind          TEXT NOT NULL,
    message       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """打开连接。文件库启用 WAL 以支持并发裁决场景的读写。"""
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection):
    """可重入事务:外层已开启事务时直接加入,便于服务间组合。"""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def all_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def insert(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    cur = conn.execute(sql, params)
    return cur.lastrowid


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区(ISO 8601 with timezone)")
    return dt.isoformat()


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def now_iso(clock) -> str:
    return to_iso(clock.now())


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str):
    return json.loads(text)
