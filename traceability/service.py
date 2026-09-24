"""批次分级追溯领域服务。

所有写操作都是：角色校验 → 事务内前置检查 → 追加不可变事件。
读模型由 traceability.state 重放得到。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from .chain import verify_lot
from .errors import (
    AuthorizationError,
    ConditionalReleaseError,
    DomainError,
    ImmutableHistoryError,
    ReleaseBlocked,
    SamplePlanViolation,
    ValidationError,
)
from .events import (
    BOX_PACKED, DECISION_ISSUED, DECISION_RECALLED, HANDOVER_RECORDED,
    INSPECTION_SUBMITTED, LOT_REGISTERED, ORDER_DELIVERED, ORDER_NOTIFIED,
    ORDER_REGISTERED, RULE_PUBLISHED, SAMPLE_COLLECTED, SEAL_RECORDED, Event,
)
from .roles import Role, Stage, STAGE_ROLE
from .rules import AcceptanceRule, RuleRegistry, Verdict, evaluate_grade
from .state import LotState, rebuild_registry, replay
from .store import Store
from .timeutil import iso, parse


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class QualityTraceService:
    def __init__(self, store: Store):
        self.store = store

    # ============================== 基础 ==============================
    def register_user(self, user_id: str, name: str, role: Role) -> None:
        self.store.register_user(user_id, name, role)

    def _role(self, user_id: str) -> Role:
        return self.store.user_role(user_id)

    def _require_role(self, user_id: str, stage: Stage) -> Role:
        role = self._role(user_id)
        expected = STAGE_ROLE[stage]
        if role != expected:
            raise AuthorizationError(
                f"{stage.value} 环节只能由 {expected.value} 签署，当前用户角色为 {role.value}"
            )
        return role

    def _lot(self, lot_no: str) -> LotState:
        state = replay(self.store.events_for(lot_no))
        if lot_no not in state:
            raise ValidationError(f"批次不存在：{lot_no}")
        return state[lot_no]

    def _registry(self) -> RuleRegistry:
        return rebuild_registry(self.store.all_events())

    def _append(self, event: Event, check=None) -> Event:
        return self.store.append(event, check)

    # ============================== 抽样规则 ==============================
    def publish_rule(self, actor: str, rule: AcceptanceRule) -> Event:
        """发布不可变规则版本；同 rule_id+version 不可重复发布。"""
        self._require_role(actor, Stage.RELEASE)  # 规则由质量管理方发布
        event = Event(
            event_id=_new_id("evt-rule"),
            event_type=RULE_PUBLISHED,
            aggregate_id=rule.rule_id,
            occurred_at=iso(rule.published_at or datetime.now().astimezone()),
            actor=actor,
            payload={
                "version": rule.version,
                "commodity": rule.commodity,
                "season": rule.season,
                "contract_no": rule.contract_no,
                "effective_from": iso(rule.effective_from),
                "min_boxes": rule.min_boxes,
                "max_boxes": rule.max_boxes,
                "min_units": rule.min_units,
                "levels": [
                    {"name": lv.name, "max_defect_rate": lv.max_defect_rate, "verdict": lv.verdict.value}
                    for lv in rule.levels
                ],
                "supersedes_version": rule.supersedes_version,
                "note": rule.note,
            },
        )
        return self._append(event)

    # ============================== 产地：登记与装箱 ==============================
    def register_lot(
        self,
        actor: str,
        lot_no: str,
        commodity: str,
        season: str,
        contract_no: str,
        quantity_boxes: int,
        occurred_at: str | datetime,
        origin_grade_table: str = "",
        event_id: str | None = None,
    ) -> Event:
        self._require_role(actor, Stage.PACK)
        if quantity_boxes <= 0:
            raise ValidationError("批次箱数必须大于 0")

        def check(conn):
            if conn.execute(
                "select 1 from events where event_type='lot.registered' and aggregate_id=?",
                (lot_no,),
            ).fetchone():
                raise ImmutableHistoryError(f"批次 {lot_no} 已登记")

        return self._append(
            Event(
                event_id=event_id or _new_id("evt-lot"),
                event_type=LOT_REGISTERED,
                aggregate_id=lot_no,
                occurred_at=iso(occurred_at),
                actor=actor,
                payload={
                    "commodity": commodity,
                    "season": season,
                    "contract_no": contract_no,
                    "quantity_boxes": quantity_boxes,
                    "origin_grade_table": origin_grade_table,
                },
            ),
            check,
        )

    def pack_box(
        self,
        actor: str,
        lot_no: str,
        box_no: str,
        declared_grade: str,
        occurred_at: str | datetime,
        event_id: str | None = None,
    ) -> Event:
        self._require_role(actor, Stage.PACK)

        def check(conn):
            if not conn.execute(
                "select 1 from events where event_type='lot.registered' and aggregate_id=?",
                (lot_no,),
            ).fetchone():
                raise ValidationError(f"批次不存在：{lot_no}")
            if conn.execute(
                "select 1 from events where event_type='box.packed' "
                "and json_extract(payload, '$.box_no')=?",
                (box_no,),
            ).fetchone():
                raise ImmutableHistoryError(f"箱号 {box_no} 已装箱，不能重复登记")

        return self._append(
            Event(
                event_id=event_id or _new_id("evt-box"),
                event_type=BOX_PACKED,
                aggregate_id=lot_no,
                occurred_at=iso(occurred_at),
                actor=actor,
                payload={"box_no": box_no, "declared_grade": declared_grade},
            ),
            check,
        )

    # ============================== 封识与到港交接 ==============================
    def record_seal(
        self,
        actor: str,
        lot_no: str,
        seal_no: str,
        occurred_at: str | datetime,
        intact: bool = True,
        note: str = "",
        event_id: str | None = None,
    ) -> Event:
        self._require_role(actor, Stage.SEAL)
        lot = self._lot(lot_no)
        if lot.seal_no is not None:
            raise ImmutableHistoryError("批次已施加封识，封识号不可更改")
        if len(lot.boxes) < lot.declared_boxes:
            raise ValidationError(
                f"封识前必须完成全部 {lot.declared_boxes} 箱装箱，当前仅 {len(lot.boxes)} 箱"
            )

        return self._append(
            Event(
                event_id=event_id or _new_id("evt-seal"),
                event_type=SEAL_RECORDED,
                aggregate_id=lot_no,
                occurred_at=iso(occurred_at),
                actor=actor,
                payload={"seal_no": seal_no, "intact": intact, "note": note},
            ),
        )

    def record_handover(
        self,
        actor: str,
        lot_no: str,
        found_seal_no: str,
        occurred_at: str | datetime,
        intact: bool = True,
        note: str = "",
        event_id: str | None = None,
    ) -> Event:
        """到港交接核对封识。封识不符照样记录（这是阻断放行的证据）。"""
        self._require_role(actor, Stage.HANDOVER)
        lot = self._lot(lot_no)
        if lot.seal_no is None:
            raise ValidationError("到港交接前必须已有封识记录")
        if lot.handover is not None:
            raise ImmutableHistoryError("到港交接只能记录一次")
        matched = lot.seal_no == found_seal_no

        return self._append(
            Event(
                event_id=event_id or _new_id("evt-handover"),
                event_type=HANDOVER_RECORDED,
                aggregate_id=lot_no,
                occurred_at=iso(occurred_at),
                actor=actor,
                payload={
                    "expected_seal_no": lot.seal_no,
                    "found_seal_no": found_seal_no,
                    "matched": matched,
                    "intact": intact and not lot.seal_broken,
                    "note": note,
                },
            ),
        )

    # ============================== 订单 ==============================
    def register_order(
        self,
        actor: str,
        lot_no: str,
        order_no: str,
        customer: str,
        qty: int,
        event_id: str | None = None,
    ) -> Event:
        self._require_role(actor, Stage.RELEASE)
        if qty <= 0:
            raise ValidationError("订单数量必须大于 0")

        def check(conn):
            if not conn.execute(
                "select 1 from events where event_type='lot.registered' and aggregate_id=?",
                (lot_no,),
            ).fetchone():
                raise ValidationError(f"批次不存在：{lot_no}")
            if conn.execute(
                "select 1 from events where event_type='order.registered' and aggregate_id=? "
                "and json_extract(payload, '$.order_no')=?",
                (lot_no, order_no),
            ).fetchone():
                raise ImmutableHistoryError(f"订单 {order_no} 已登记")

        return self._append(
            Event(
                event_id=event_id or _new_id("evt-order"),
                event_type=ORDER_REGISTERED,
                aggregate_id=lot_no,
                occurred_at=datetime.now().astimezone().isoformat(),
                actor=actor,
                payload={"order_no": order_no, "customer": customer, "qty": qty},
            ),
            check,
        )

    # ============================== 抽样 ==============================
    def _stage_for_kind(self, kind: str) -> Stage:
        if kind in ("initial", "backfill-market"):
            return Stage.MARKET_SAMPLE
        if kind in ("reinspection", "backfill-customer"):
            return Stage.CUSTOMER_SAMPLE
        raise ValidationError(f"未知抽样类型：{kind}")

    def collect_sample(
        self,
        actor: str,
        lot_no: str,
        round_no: int,
        kind: str,
        boxes: Sequence[str],
        total_units: int,
        occurred_at: str | datetime,
        recorded_at: str | datetime | None = None,
        event_id: str | None = None,
    ) -> tuple[Event, AcceptanceRule]:
        """登记抽样。按品种/产季/客户合同选择业务发生时刻已生效的规则版本。

        recorded_at 晚于 occurred_at 即为补录；补录不改变任何历史结论。
        """
        stage = self._stage_for_kind(kind)
        self._require_role(actor, stage)
        lot = self._lot(lot_no)
        if round_no <= 0:
            raise ValidationError("轮次号必须从 1 开始")
        if round_no in lot.samples:
            raise ImmutableHistoryError(f"第 {round_no} 轮抽样已存在")
        boxes = list(boxes)
        if not boxes or len(set(boxes)) != len(boxes):
            raise SamplePlanViolation("抽样箱号不能为空且不能重复")
        unknown = [b for b in boxes if b not in lot.boxes]
        if unknown:
            raise SamplePlanViolation(f"抽样箱不属于本批次：{'、'.join(unknown)}")
        rule = self._registry().select(
            lot.commodity, lot.season, lot.contract_no, occurred_at
        )
        if not (rule.min_boxes <= len(boxes) <= rule.max_boxes):
            raise SamplePlanViolation(
                f"规则 {rule.rule_id} v{rule.version} 要求抽样 {rule.min_boxes}~"
                f"{rule.max_boxes} 箱，实际 {len(boxes)} 箱"
            )
        if total_units < rule.min_units:
            raise SamplePlanViolation(
                f"规则 {rule.rule_id} v{rule.version} 要求至少 {rule.min_units} 个样本单位，"
                f"实际 {total_units}"
            )

        event = Event(
            event_id=event_id or _new_id("evt-sample"),
            event_type=SAMPLE_COLLECTED,
            aggregate_id=lot_no,
            occurred_at=iso(occurred_at),
            recorded_at=iso(recorded_at) if recorded_at else None,
            actor=actor,
            payload={
                "round": round_no,
                "kind": kind,
                "boxes": boxes,
                "total_units": total_units,
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "backfilled": recorded_at is not None
                and parse(recorded_at) > parse(occurred_at),
            },
        )
        return self._append(event), rule

    # ============================== 检验与裁决版本 ==============================
    def submit_inspection(
        self,
        actor: str,
        lot_no: str,
        round_no: int,
        kind: str,
        defective_units: int,
        total_units: int,
        occurred_at: str | datetime,
        conclusion: str = "",
        recorded_at: str | datetime | None = None,
        event_id: str | None = None,
    ) -> Event:
        """提交检验结论。

        - initial：市场初检（第 1 轮）；
        - reinspection：客户复检，必须基于已存在的上一轮；结论变化即产生新版本；
        - backfill-*：事后补录，按业务发生时生效的规则定级，结论文本逐字保留。
        多个检验员同时提交同一轮：数据库唯一约束保证只有一次有效裁决。
        """
        if kind in ("initial", "backfill-market"):
            self._require_role(actor, Stage.MARKET_INSPECTION)
            expected_round = 1
            supersedes: int | None = None
        elif kind in ("reinspection", "backfill-customer"):
            self._require_role(actor, Stage.CUSTOMER_INSPECTION)
            lot = self._lot(lot_no)
            if not lot.inspections:
                raise ValidationError("复检必须在初检之后")
            expected_round = max(lot.inspections) + 1
            supersedes = max(lot.inspections)
        else:
            raise ValidationError(f"未知检验类型：{kind}")

        if round_no != expected_round:
            raise ValidationError(
                f"第 {expected_round} 轮尚未形成，不能直接提交第 {round_no} 轮检验"
            )
        if defective_units < 0 or defective_units > total_units:
            raise ValidationError("不合格单位数必须在 0 与样本总数之间")

        lot = self._lot(lot_no)
        sample = lot.samples.get(round_no)
        if sample is None:
            raise SamplePlanViolation(f"第 {round_no} 轮缺少抽样记录，不能出具检验结论")
        if total_units != sample.total_units:
            raise SamplePlanViolation(
                f"检验样本数 {total_units} 与抽样登记 {sample.total_units} 不一致"
            )
        rule = self._registry().select(
            lot.commodity, lot.season, lot.contract_no, occurred_at
        )
        if (rule.rule_id, rule.version) != (sample.rule_id, sample.rule_version):
            raise SamplePlanViolation(
                f"检验适用规则 {rule.rule_id} v{rule.version} 与抽样锁定的 "
                f"{sample.rule_id} v{sample.rule_version} 不一致"
            )
        grade, verdict, rate = evaluate_grade(rule, defective_units, total_units)
        backfilled = recorded_at is not None and parse(recorded_at) > parse(occurred_at)

        event = Event(
            event_id=event_id or _new_id("evt-insp"),
            event_type=INSPECTION_SUBMITTED,
            aggregate_id=lot_no,
            occurred_at=iso(occurred_at),
            recorded_at=iso(recorded_at) if recorded_at else None,
            actor=actor,
            payload={
                "round": round_no,
                "kind": kind,
                "grade": grade,
                "verdict": verdict.value,
                "defect_rate": round(rate, 6),
                "defective_units": defective_units,
                "total_units": total_units,
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "supersedes_round": supersedes,
                "conclusion": conclusion,
                "backfilled": backfilled,
            },
        )
        stored = self._append(event)

        # 复检（含事后补录的复检）推翻前轮：只通知/标记仍未交付的受影响订单
        if supersedes is not None:
            previous = lot.inspections[supersedes]
            overturned = previous.grade != grade or previous.verdict != verdict.value
            if overturned:
                self._notify_undelivered(
                    lot_no, round_no, "overturn",
                    f"第 {round_no} 轮复检推翻第 {supersedes} 轮结论："
                    f"{previous.grade}/{previous.verdict} → {grade}/{verdict.value}",
                )
        return stored

    # ============================== 放行裁决 ==============================
    def issue_decision(
        self,
        actor: str,
        lot_no: str,
        release_type: str,
        basis_round: int,
        occurred_at: str | datetime,
        qty_limit: int | None = None,
        valid_until: str | datetime | None = None,
        reason: str = "",
        decision_no: str | None = None,
    ) -> Event:
        self._require_role(actor, Stage.RELEASE)
        if release_type not in ("full", "conditional", "rejected"):
            raise ValidationError("裁决类型必须是 full / conditional / rejected")
        lot = self._lot(lot_no)
        inspection = lot.inspections.get(basis_round)
        if inspection is None:
            raise ValidationError(f"第 {basis_round} 轮检验结论不存在，不能放行")

        # 封识不符或证据缺口 → 直接阻断任何放行
        report = verify_lot(lot)
        if release_type in ("full", "conditional") and not report.clear:
            raise ReleaseBlocked(report.gaps)

        if release_type == "full" and inspection.verdict != Verdict.QUALIFIED.value:
            raise ReleaseBlocked(
                [f"第 {basis_round} 轮裁决为 {inspection.verdict}，不能无条件放行"]
            )
        if release_type == "conditional":
            if inspection.verdict == Verdict.REJECTED.value:
                raise ReleaseBlocked(["检验结论为拒收，不能有条件放行"])
            if qty_limit is None or qty_limit <= 0:
                raise ConditionalReleaseError("有条件放行必须限定数量（qty_limit > 0）")
            remaining = lot.declared_boxes - self._delivered_qty(lot)
            if qty_limit > remaining:
                raise ConditionalReleaseError(
                    f"有条件放行数量 {qty_limit} 超出批次剩余可放行数量 {remaining}"
                )
            if valid_until is None:
                raise ConditionalReleaseError("有条件放行必须设定有效期 valid_until")
            if parse(valid_until) <= parse(occurred_at):
                raise ConditionalReleaseError("有效期必须晚于放行时间")

        event = Event(
            event_id=_new_id("evt-decision"),
            event_type=DECISION_ISSUED,
            aggregate_id=lot_no,
            occurred_at=iso(occurred_at),
            actor=actor,
            payload={
                "decision_no": decision_no or _new_id("DEC"),
                "release_type": release_type,
                "basis_round": basis_round,
                "qty_limit": qty_limit,
                "valid_until": iso(valid_until) if valid_until else None,
                "reason": reason,
            },
        )
        stored = self._append(event)
        if release_type in ("full", "conditional"):
            self._notify_undelivered(
                lot_no, basis_round, "release",
                f"批次已{('有条件' if release_type == 'conditional' else '')}放行，"
                f"依据第 {basis_round} 轮检验",
            )
        return stored

    def recall_decision(
        self,
        actor: str,
        lot_no: str,
        decision_no: str,
        reason: str,
        occurred_at: str | datetime,
        event_id: str | None = None,
    ) -> Event:
        """召回：有条件放行期满、或后续检验不合格时可执行。"""
        self._require_role(actor, Stage.RELEASE)
        lot = self._lot(lot_no)
        target = next((d for d in lot.decisions if d.decision_no == decision_no), None)
        if target is None:
            raise ValidationError(f"裁决不存在：{decision_no}")
        if not target.active:
            raise ImmutableHistoryError("裁决已被召回，不能重复召回")

        now = parse(occurred_at)
        expired = target.valid_until is not None and now > parse(target.valid_until)
        later_bad = False
        current = lot.current_inspection
        if current is not None and current.round > target.basis_round:
            later_bad = current.verdict in (Verdict.REJECTED.value, Verdict.CONDITIONAL.value)
        if not (expired or later_bad):
            raise DomainError(
                "召回条件不成立：放行仍在有效期内，且没有后续不合格结论"
            )

        stored = self._append(
            Event(
                event_id=event_id or _new_id("evt-recall"),
                event_type=DECISION_RECALLED,
                aggregate_id=lot_no,
                occurred_at=iso(now),
                actor=actor,
                payload={"decision_no": decision_no, "reason": reason,
                         "trigger": "expired" if expired else "subsequent_failure"},
            )
        )
        self._notify_undelivered(lot_no, target.basis_round, "recall", reason)
        return stored

    # ============================== 交付 ==============================
    def mark_delivered(
        self,
        actor: str,
        lot_no: str,
        order_no: str,
        qty: int,
        occurred_at: str | datetime,
        event_id: str | None = None,
    ) -> Event:
        self._require_role(actor, Stage.RELEASE)
        lot = self._lot(lot_no)
        if order_no not in lot.orders:
            raise ValidationError(f"订单未登记：{order_no}")
        if lot.orders[order_no].delivered:
            raise ImmutableHistoryError("订单已交付")
        decision = lot.effective_decision()
        if decision is None:
            raise ReleaseBlocked(["批次没有生效中的放行裁决，禁止交付"])
        # 放行依据的轮次之后又出现非合格结论（复检改判）：在召回/重裁前阻断交付
        current = lot.current_inspection
        if (
            current is not None
            and current.round > decision.basis_round
            and current.verdict != Verdict.QUALIFIED.value
        ):
            raise ReleaseBlocked([
                f"第 {current.round} 轮复检结论为 {current.verdict}，"
                "原放行裁决已被新结论动摇，需召回后重新裁决"
            ])
        if decision.release_type == "conditional":
            if parse(occurred_at) > parse(decision.valid_until):
                raise ReleaseBlocked(["有条件放行已过有效期，禁止交付"])
            delivered_total = self._delivered_qty(lot)
            if delivered_total + qty > (decision.qty_limit or 0):
                raise ConditionalReleaseError(
                    f"交付数量 {delivered_total + qty} 超过有条件放行限额 "
                    f"{decision.qty_limit}"
                )

        return self._append(
            Event(
                event_id=event_id or _new_id("evt-deliver"),
                event_type=ORDER_DELIVERED,
                aggregate_id=lot_no,
                occurred_at=iso(occurred_at),
                actor=actor,
                payload={"order_no": order_no, "qty": qty},
            ),
        )

    # ============================== 内部辅助 ==============================
    @staticmethod
    def _delivered_qty(lot: LotState) -> int:
        return sum(o.delivered_qty for o in lot.orders.values() if o.delivered)

    def _notify_undelivered(
        self, lot_no: str, round_no: int, kind: str, message: str
    ) -> None:
        """只对仍未交付的订单追加通知；已交付订单不受影响、不被标记。"""
        lot = self._lot(lot_no)
        for order in lot.orders.values():
            if order.delivered:
                continue
            self._append(
                Event(
                    event_id=_new_id("evt-notice"),
                    event_type=ORDER_NOTIFIED,
                    aggregate_id=lot_no,
                    occurred_at=datetime.now().astimezone().isoformat(),
                    actor="system",
                    payload={
                        "order_no": order.order_no,
                        "customer": order.customer,
                        "round": round_no,
                        "kind": kind,
                        "message": message,
                    },
                )
            )
