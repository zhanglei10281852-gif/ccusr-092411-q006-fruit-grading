"""证据链:产地装箱 → 集装箱封识 → 到港交接 → 市场抽检 → 客户复检。

不变量:
- 每个环节只能由 STAGE_ROLE 指定的角色签署,且每批次每环节只签署一次;
- 后一环节必须引用前一环节的证据(prev_event_id),形成可验证的链式结构;
- 到港交接如实记录封识核验结果,是否一致由 verify_chain 判定,
  判定结果用于放行阻断与审计。

各环节 payload 约定:
- PACKING:               {"origin": str, "containers": [{"container_no", "box_count"}]}
- SEALING:               {"seals": [{"container_no", "seal_no"}]}
- PORT_HANDOVER:         {"port": str, "checks": [{"container_no", "seal_no", "intact"}]}
- MARKET_SAMPLING:       {"location": str, "sample_box_ids": [box_no, ...]}
- CUSTOMER_REINSPECTION: {"customer": str, "sample_box_ids": [box_no, ...]}
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from .. import db
from ..constants import (
    RELEASE_REQUIRED_STAGES,
    STAGE_ORDER,
    STAGE_ROLE,
    STAGE_TO_LOT_STATE,
    Stage,
)
from ..errors import (
    DomainError,
    EvidenceGapError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..lots import advance_lot_state


@dataclass
class ChainReport:
    """证据链核验结果。ok 为 False 时放行必须被阻断。"""

    lot_id: int
    events: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)  # 证据缺口
    seal_mismatches: list[str] = field(default_factory=list)  # 封识不符
    violations: list[str] = field(default_factory=list)  # 链式引用/签署角色异常

    @property
    def ok(self) -> bool:
        return not (self.gaps or self.seal_mismatches or self.violations)


def parse_event(event: dict) -> dict:
    event = dict(event)
    event["payload"] = db.loads(event["payload"])
    return event


class EvidenceService:
    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock

    # ---------- 签署 ----------
    def record_event(self, lot_id: int, stage, actor_id: int, payload: dict, occurred_at=None) -> dict:
        """签署一个环节的证据。角色不符、前置环节缺失或重复签署都会被拒绝。"""
        stage = Stage(stage)
        occurred = db.to_iso(occurred_at) if occurred_at is not None else db.now_iso(self.clock)
        with db.tx(self.conn):
            lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (lot_id,))
            if lot is None:
                raise NotFoundError(f"批次不存在: {lot_id}")
            actor = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (actor_id,))
            if actor is None:
                raise NotFoundError(f"用户不存在: {actor_id}")
            expected = STAGE_ROLE[stage]
            if actor["role"] != expected.value:
                raise PermissionDeniedError(
                    f"环节「{stage.value}」只能由 {expected.value} 签署,"
                    f"{actor['name']} 的角色是 {actor['role']}"
                )
            self._validate_payload(lot_id, stage, payload)
            prev_id = None
            idx = STAGE_ORDER.index(stage)
            if idx > 0:
                prev_stage = STAGE_ORDER[idx - 1]
                prev = db.one(
                    self.conn,
                    "SELECT * FROM evidence_events WHERE lot_id = ? AND stage = ?",
                    (lot_id, prev_stage.value),
                )
                if prev is None:
                    raise EvidenceGapError(
                        f"缺少前置环节「{prev_stage.value}」的证据,无法签署「{stage.value}」"
                    )
                prev_id = prev["id"]
            signature = hashlib.sha256(
                f"{actor_id}|{stage.value}|{db.dumps(payload)}|{occurred}".encode("utf-8")
            ).hexdigest()
            try:
                event_id = db.insert(
                    self.conn,
                    "INSERT INTO evidence_events(lot_id, stage, actor_id, occurred_at,"
                    " recorded_at, prev_event_id, payload, signature)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lot_id,
                        stage.value,
                        actor_id,
                        occurred,
                        db.now_iso(self.clock),
                        prev_id,
                        db.dumps(payload),
                        signature,
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError(
                    f"批次 {lot['code']} 的环节「{stage.value}」已签署,证据不可篡改;"
                    f"如需更正请通过复检产生新版本"
                ) from None
            advance_lot_state(self.conn, lot_id, STAGE_TO_LOT_STATE[stage])
        return self.get_event(event_id)

    # ---------- 查询 ----------
    def get_event(self, event_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM evidence_events WHERE id = ?", (event_id,))
        if row is None:
            raise NotFoundError(f"证据不存在: {event_id}")
        return parse_event(row)

    def chain_events(self, lot_id: int) -> list[dict]:
        """按环节顺序返回批次已签署的证据。"""
        rows = db.all_rows(
            self.conn, "SELECT * FROM evidence_events WHERE lot_id = ?", (lot_id,)
        )
        order = {stage.value: i for i, stage in enumerate(STAGE_ORDER)}
        return [parse_event(r) for r in sorted(rows, key=lambda r: order[r["stage"]])]

    def stage_event(self, lot_id: int, stage) -> dict | None:
        row = db.one(
            self.conn,
            "SELECT * FROM evidence_events WHERE lot_id = ? AND stage = ?",
            (lot_id, Stage(stage).value),
        )
        return parse_event(row) if row else None

    # ---------- 核验 ----------
    def verify_chain(self, lot_id: int) -> ChainReport:
        """核验证据链:环节是否齐全、链式引用是否正确、签署角色是否匹配、封识是否一致。"""
        events = self.chain_events(lot_id)
        by_stage = {e["stage"]: e for e in events}
        report = ChainReport(lot_id=lot_id, events=events)

        for stage in RELEASE_REQUIRED_STAGES:
            if stage.value not in by_stage:
                report.gaps.append(f"缺少环节「{stage.value}」的证据")

        for i, stage in enumerate(STAGE_ORDER):
            event = by_stage.get(stage.value)
            if event is None:
                continue
            actor = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (event["actor_id"],))
            if actor is None or actor["role"] != STAGE_ROLE[stage].value:
                report.violations.append(
                    f"环节「{stage.value}」的签署角色不符(actor_id={event['actor_id']})"
                )
            if i > 0:
                prev = by_stage.get(STAGE_ORDER[i - 1].value)
                if prev is not None and event["prev_event_id"] != prev["id"]:
                    report.violations.append(
                        f"环节「{stage.value}」未正确引用前一环节「{STAGE_ORDER[i - 1].value}」的证据"
                    )

        sealing = by_stage.get(Stage.SEALING.value)
        handover = by_stage.get(Stage.PORT_HANDOVER.value)
        if sealing and handover:
            seals = {s["container_no"]: s["seal_no"] for s in sealing["payload"]["seals"]}
            for check in handover["payload"]["checks"]:
                container_no = check["container_no"]
                if container_no not in seals:
                    report.seal_mismatches.append(
                        f"集装箱 {container_no} 在产地封识记录中不存在"
                    )
                    continue
                if check["seal_no"] != seals[container_no]:
                    report.seal_mismatches.append(
                        f"集装箱 {container_no} 封识号不符:产地 {seals[container_no]},"
                        f"到港核验 {check['seal_no']}"
                    )
                if not check.get("intact", False):
                    report.seal_mismatches.append(f"集装箱 {container_no} 到港时封识已破损")
        return report

    # ---------- payload 校验 ----------
    def _validate_payload(self, lot_id: int, stage: Stage, payload: dict) -> None:
        containers = db.all_rows(
            self.conn, "SELECT * FROM containers WHERE lot_id = ?", (lot_id,)
        )
        registered = {c["container_no"] for c in containers}

        def require_full_coverage(entries, key, label):
            seen = set()
            for entry in entries:
                container_no = entry.get("container_no")
                if container_no not in registered:
                    raise ValidationError(f"{label}中出现未登记的集装箱: {container_no}")
                seen.add(container_no)
            missing = registered - seen
            if missing:
                raise ValidationError(f"{label}未覆盖批次全部集装箱,缺少: {sorted(missing)}")

        if stage is Stage.PACKING:
            entries = payload.get("containers")
            if not entries:
                raise ValidationError("装箱证据必须包含 containers 清单")
            require_full_coverage(entries, "container_no", "装箱单")
            counts = {c["container_no"]: c.get("box_count") for c in entries}
            for container in containers:
                declared = counts[container["container_no"]]
                actual = db.one(
                    self.conn,
                    "SELECT COUNT(*) AS n FROM boxes WHERE container_id = ?",
                    (container["id"],),
                )["n"]
                if declared != actual:
                    raise ValidationError(
                        f"装箱单中集装箱 {container['container_no']} 申报 {declared} 箱,"
                        f"与登记的 {actual} 箱不符"
                    )
        elif stage is Stage.SEALING:
            seals = payload.get("seals")
            if not seals:
                raise ValidationError("封识证据必须包含 seals 清单")
            require_full_coverage(seals, "container_no", "封识记录")
            for seal in seals:
                if not seal.get("seal_no"):
                    raise ValidationError("每个集装箱都必须有封识号")
        elif stage is Stage.PORT_HANDOVER:
            checks = payload.get("checks")
            if not checks:
                raise ValidationError("交接证据必须包含 checks 清单")
            require_full_coverage(checks, "container_no", "交接核验记录")
            for check in checks:
                if not check.get("seal_no"):
                    raise ValidationError("交接核验必须记录每个集装箱的封识号")
                if not isinstance(check.get("intact"), bool):
                    raise ValidationError("交接核验必须记录封识是否完好(intact)")
        elif stage in (Stage.MARKET_SAMPLING, Stage.CUSTOMER_REINSPECTION):
            sample_box_ids = payload.get("sample_box_ids")
            if not sample_box_ids:
                raise ValidationError("抽样证据必须包含 sample_box_ids")
            for box_no in sample_box_ids:
                row = db.one(
                    self.conn,
                    "SELECT id FROM boxes WHERE box_no = ? AND lot_id = ?",
                    (box_no, lot_id),
                )
                if row is None:
                    raise ValidationError(f"抽样箱 {box_no} 不属于本批次")
