"""状态重放：从事件序列还原批次全貌。

重放只依赖事件，因此：
- 补录事件按业务时间插入历史，但历史结论以当时已存在的事件计算，不会被改写；
- 复检以新一轮事件追加，current_round 前移即"新版本"，旧版本完整保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import (
    BOX_PACKED, DECISION_ISSUED, DECISION_RECALLED, HANDOVER_RECORDED,
    INSPECTION_SUBMITTED, LOT_REGISTERED, ORDER_DELIVERED, ORDER_NOTIFIED,
    ORDER_REGISTERED, RULE_PUBLISHED, SAMPLE_COLLECTED, SEAL_BROKEN, SEAL_RECORDED,
    Event,
)
from .rules import AcceptanceRule, GradeLevel, RuleRegistry, Verdict
from .timeutil import parse


@dataclass
class SampleRecord:
    round: int
    kind: str                       # initial / reinspection / backfill
    box_seq: list[str]
    total_units: int
    rule_id: str
    rule_version: int
    event: Event


@dataclass
class InspectionRecord:
    round: int
    kind: str
    grade: str
    verdict: str
    defect_rate: float
    defective_units: int
    total_units: int
    rule_id: str
    rule_version: int
    supersedes_round: int | None
    conclusion: str                  # 当时结论文本
    event: Event

    @property
    def is_reinspection(self) -> bool:
        return self.supersedes_round is not None


@dataclass
class DecisionRecord:
    decision_no: str
    release_type: str                # full / conditional / rejected
    basis_round: int
    qty_limit: int | None
    valid_until: str | None
    reason: str
    recalled: bool = False
    recall_reason: str | None = None
    event: Event | None = None
    recall_event: Event | None = None

    @property
    def active(self) -> bool:
        return not self.recalled


@dataclass
class OrderState:
    order_no: str
    qty: int
    customer: str = ""
    notified_rounds: list[int] = field(default_factory=list)
    notices: list[Event] = field(default_factory=list)
    delivered: bool = False
    delivered_at: str | None = None
    delivered_qty: int = 0


@dataclass
class LotState:
    lot_no: str
    commodity: str = ""
    season: str = ""
    contract_no: str = ""
    declared_boxes: int = 0
    boxes: dict[str, dict[str, Any]] = field(default_factory=dict)
    seal_no: str | None = None
    sealed_at: str | None = None
    seal_broken: bool = False
    seal_notes: list[str] = field(default_factory=list)
    handover: dict[str, Any] | None = None
    samples: dict[int, SampleRecord] = field(default_factory=dict)
    inspections: dict[int, InspectionRecord] = field(default_factory=dict)
    decisions: list[DecisionRecord] = field(default_factory=list)
    orders: dict[str, OrderState] = field(default_factory=dict)
    created_seq: int | None = None

    # ---- 版本视图 ----
    @property
    def current_round(self) -> int | None:
        return max(self.inspections) if self.inspections else None

    @property
    def current_inspection(self) -> InspectionRecord | None:
        r = self.current_round
        return self.inspections[r] if r is not None else None

    def grade_history(self) -> list[InspectionRecord]:
        return [self.inspections[k] for k in sorted(self.inspections)]

    def effective_decision(self) -> DecisionRecord | None:
        """当前生效裁决：最后一个未被召回的放行裁决。"""
        for decision in reversed(self.decisions):
            if decision.active and decision.release_type in ("full", "conditional"):
                return decision
        return None

    def undelivered_orders(self) -> list[OrderState]:
        return [o for o in self.orders.values() if not o.delivered]


def rebuild_registry(events: list[Event]) -> RuleRegistry:
    registry = RuleRegistry()
    for event in events:
        if event.event_type != RULE_PUBLISHED:
            continue
        p = event.payload
        levels = tuple(
            GradeLevel(
                name=lv["name"],
                max_defect_rate=float(lv["max_defect_rate"]),
                verdict=Verdict(lv["verdict"]),
            )
            for lv in p["levels"]
        )
        registry.publish(
            AcceptanceRule(
                rule_id=event.aggregate_id,
                version=p["version"],
                commodity=p["commodity"],
                season=p["season"],
                contract_no=p["contract_no"],
                effective_from=parse(p["effective_from"]),
                levels=levels,
                min_boxes=p["min_boxes"],
                max_boxes=p["max_boxes"],
                min_units=p["min_units"],
                supersedes_version=p.get("supersedes_version"),
                published_at=parse(event.occurred_at),
                note=p.get("note", ""),
            )
        )
    return registry


def apply_event(state: LotState, event: Event) -> None:
    p = event.payload
    t = event.event_type
    if t == LOT_REGISTERED:
        state.commodity = p["commodity"]
        state.season = p["season"]
        state.contract_no = p["contract_no"]
        state.declared_boxes = p["quantity_boxes"]
        state.created_seq = event.seq
    elif t == BOX_PACKED:
        state.boxes[p["box_no"]] = {
            "declared_grade": p.get("declared_grade", ""),
            "packed_at": event.occurred_at,
            "seq": event.seq,
        }
    elif t == SEAL_RECORDED:
        state.seal_no = p["seal_no"]
        state.sealed_at = event.occurred_at
        if not p.get("intact", True):
            state.seal_broken = True
        state.seal_notes.append(p.get("note", ""))
    elif t == SEAL_BROKEN:
        state.seal_broken = True
        state.seal_notes.append(p.get("note", "封识异常"))
    elif t == HANDOVER_RECORDED:
        state.handover = {
            "expected_seal_no": p["expected_seal_no"],
            "found_seal_no": p["found_seal_no"],
            "matched": p["matched"],
            "intact": p.get("intact", True),
            "at": event.occurred_at,
            "note": p.get("note", ""),
        }
    elif t == SAMPLE_COLLECTED:
        sample = SampleRecord(
            round=p["round"],
            kind=p["kind"],
            box_seq=list(p["boxes"]),
            total_units=p["total_units"],
            rule_id=p["rule_id"],
            rule_version=p["rule_version"],
            event=event,
        )
        state.samples[sample.round] = sample
    elif t == INSPECTION_SUBMITTED:
        ins = InspectionRecord(
            round=p["round"],
            kind=p["kind"],
            grade=p["grade"],
            verdict=p["verdict"],
            defect_rate=float(p["defect_rate"]),
            defective_units=p["defective_units"],
            total_units=p["total_units"],
            rule_id=p["rule_id"],
            rule_version=p["rule_version"],
            supersedes_round=p.get("supersedes_round"),
            conclusion=p.get("conclusion", ""),
            event=event,
        )
        state.inspections[ins.round] = ins
    elif t == DECISION_ISSUED:
        state.decisions.append(
            DecisionRecord(
                decision_no=p["decision_no"],
                release_type=p["release_type"],
                basis_round=p["basis_round"],
                qty_limit=p.get("qty_limit"),
                valid_until=p.get("valid_until"),
                reason=p.get("reason", ""),
                event=event,
            )
        )
    elif t == DECISION_RECALLED:
        for decision in reversed(state.decisions):
            if decision.decision_no == p["decision_no"] and decision.active:
                decision.recalled = True
                decision.recall_reason = p.get("reason", "")
                decision.recall_event = event
                break
    elif t == ORDER_REGISTERED:
        order = state.orders.setdefault(
            p["order_no"], OrderState(p["order_no"], p.get("qty", 0))
        )
        order.customer = p.get("customer", "")
        order.qty = p.get("qty", order.qty)
    elif t == ORDER_NOTIFIED:
        order = state.orders.setdefault(p["order_no"], OrderState(p["order_no"], p.get("qty", 0)))
        if p["round"] not in order.notified_rounds:
            order.notified_rounds.append(p["round"])
        order.notices.append(event)
        order.qty = order.qty or p.get("qty", 0)
        if p.get("customer"):
            order.customer = p["customer"]
    elif t == ORDER_DELIVERED:
        order = state.orders.setdefault(p["order_no"], OrderState(p["order_no"], p.get("qty", 0)))
        order.delivered = True
        order.delivered_at = event.occurred_at
        order.delivered_qty = p.get("qty", order.qty)
        order.qty = order.qty or p.get("qty", 0)


def replay(events: list[Event]) -> dict[str, LotState]:
    lots: dict[str, LotState] = {}
    for event in events:
        if event.event_type == LOT_REGISTERED:
            lots[event.aggregate_id] = LotState(lot_no=event.aggregate_id)
        lot = lots.get(event.aggregate_id)
        if lot is not None:
            apply_event(lot, event)
    return lots
