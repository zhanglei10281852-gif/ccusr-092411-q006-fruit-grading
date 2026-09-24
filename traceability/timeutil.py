"""时间工具：领域内统一使用带时区的 ISO 8601 时间。"""
from __future__ import annotations

from datetime import datetime


def parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须携带时区：{value!r}")
    return dt


def iso(value: str | datetime) -> str:
    return parse(value).isoformat()
