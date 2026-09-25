"""可注入时钟,便于测试有效期、补录等与时间相关的行为。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    """系统时钟,一律返回带时区的 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """测试与演示用手动时钟。"""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("时钟起点必须包含时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self._now = value

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)
