"""不可变事件。

系统状态完全由事件序列重放得到；补录与复检都通过追加事件实现，
历史事件永不更新或删除，因此"当时结论"可以随时重放复原。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

from .timeutil import parse

# ---- 事件类型（与 domain/contract.json 对齐并细化）----
LOT_REGISTERED = "lot.registered"                 # 产地登记批次
BOX_PACKED = "box.packed"                         # 产地装箱
SEAL_RECORDED = "seal.recorded"                   # 施加封识
SEAL_BROKEN = "seal.broken"                       # 登记封识破损/异常
HANDOVER_RECORDED = "handover.recorded"           # 到港交接（核对封识）
SAMPLE_COLLECTED = "sample.collected"             # 抽样
INSPECTION_SUBMITTED = "inspection.submitted"     # 检验提交（初检/补录/复检）
DECISION_ISSUED = "decision.issued"               # 放行裁决
DECISION_RECALLED = "decision.recalled"           # 召回
ORDER_REGISTERED = "order.registered"             # 客户订单登记
ORDER_NOTIFIED = "order.notified"                 # 客户通知
ORDER_DELIVERED = "order.delivered"               # 订单交付
RULE_PUBLISHED = "rule.published"                 # 抽样规则发布

EVENT_TYPES = frozenset({
    LOT_REGISTERED, BOX_PACKED, SEAL_RECORDED, SEAL_BROKEN, HANDOVER_RECORDED,
    SAMPLE_COLLECTED, INSPECTION_SUBMITTED, DECISION_ISSUED, DECISION_RECALLED,
    ORDER_REGISTERED, ORDER_NOTIFIED, ORDER_DELIVERED, RULE_PUBLISHED,
})


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_id: str
    occurred_at: str
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    # recorded_at：事件进入系统的时间，补录时晚于 occurred_at
    recorded_at: str | None = None
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def occurred(self) -> datetime:
        return parse(self.occurred_at)

    @property
    def recorded(self) -> datetime:
        return parse(self.recorded_at or self.occurred_at)
