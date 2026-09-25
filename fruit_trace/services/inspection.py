"""检验记录与等级结论版本化。

核心规则:
- 检验必须挂在对应环节(市场抽检/客户复检)的证据上,且样本箱来自该环节
  登记的抽样箱——否则无法证明检验对象属于本批次;
- 样本补录(检验发生时间早于当前结论的生成时间)只归档到当时生效的版本,
  保留当时结论,不产生新版本;
- 复检推翻前版结论时产生新版本,旧版作废,仅标记仍未交付的受影响订单;
- 新版结论为不合格时,自动召回仍在有效期内的放行。
"""
from __future__ import annotations

from .. import db
from ..constants import (
    INSPECTION_ROLE,
    INSPECTION_STAGE,
    Grade,
    InspectionKind,
    LotState,
)
from ..errors import (
    EvidenceGapError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..lots import advance_lot_state


class InspectionService:
    def __init__(self, conn, clock, sampling, orders, releases):
        self.conn = conn
        self.clock = clock
        self.sampling = sampling
        self.orders = orders
        self.releases = releases

    def record_inspection(
        self,
        lot_id: int,
        kind,
        inspector_id: int,
        sample_box_ids: list,
        total_fruits: int,
        defect_fruits: int,
        occurred_at=None,
        note: str = "",
    ) -> dict:
        kind = InspectionKind(kind)
        with db.tx(self.conn):
            lot = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (lot_id,))
            if lot is None:
                raise NotFoundError(f"批次不存在: {lot_id}")
            inspector = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (inspector_id,))
            if inspector is None:
                raise NotFoundError(f"用户不存在: {inspector_id}")
            expected = INSPECTION_ROLE[kind]
            if inspector["role"] != expected.value:
                raise PermissionDeniedError(
                    f"{kind.value} 只能由 {expected.value} 提交,"
                    f"{inspector['name']} 的角色是 {inspector['role']}"
                )
            if not sample_box_ids:
                raise ValidationError("检验样本箱不能为空")
            if not isinstance(total_fruits, int) or total_fruits <= 0:
                raise ValidationError("检验果数必须为正整数")
            if not isinstance(defect_fruits, int) or not 0 <= defect_fruits <= total_fruits:
                raise ValidationError("缺陷果数必须在 [0, 检验果数] 之间")
            occurred = self.clock.now() if occurred_at is None else occurred_at
            occurred_iso = db.to_iso(occurred)

            # 检验必须对应环节证据,且样本箱 ⊆ 证据登记的抽样箱
            stage = INSPECTION_STAGE[kind]
            event = db.one(
                self.conn,
                "SELECT * FROM evidence_events WHERE lot_id = ? AND stage = ?",
                (lot_id, stage.value),
            )
            if event is None:
                raise EvidenceGapError(
                    f"检验缺少「{stage.value}」环节证据,无法证明样本来自本批次"
                )
            event_boxes = set(db.loads(event["payload"]).get("sample_box_ids", []))
            unknown = [b for b in sample_box_ids if b not in event_boxes]
            if unknown:
                raise ValidationError(
                    f"样本箱未登记在「{stage.value}」证据中: {unknown}"
                )

            rule = self.sampling.select_rule(lot["variety"], lot["season"], lot["contract_id"])
            defect_rate = defect_fruits / total_fruits
            grade = self.sampling.evaluate_grade(rule, defect_rate)

            current = db.one(
                self.conn,
                "SELECT * FROM grade_conclusions WHERE lot_id = ? AND status = 'current'",
                (lot_id,),
            )
            backfilled = current is not None and occurred < db.parse_iso(current["created_at"])

            inspection_id = db.insert(
                self.conn,
                "INSERT INTO inspections(lot_id, kind, inspector_id, occurred_at, recorded_at,"
                " backfilled, sample_box_ids, total_fruits, defect_fruits, defect_rate, grade,"
                " rule_id, conclusion_version, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    lot_id,
                    kind.value,
                    inspector_id,
                    occurred_iso,
                    db.now_iso(self.clock),
                    int(backfilled),
                    db.dumps(list(sample_box_ids)),
                    total_fruits,
                    defect_fruits,
                    defect_rate,
                    grade.value,
                    rule["id"],
                    note,
                ),
            )

            if current is None:
                # 首版结论
                self._insert_conclusion(
                    lot_id,
                    version=1,
                    grade=grade,
                    inspection_id=inspection_id,
                    rule=rule,
                    defect_rate=defect_rate,
                    sample_box_ids=sample_box_ids,
                    supersedes=None,
                    reason=(
                        f"初检评定:缺陷率 {defect_rate:.2%},"
                        f"依据规则「{rule['name']}」判定 {grade.value}"
                    ),
                )
                self._set_inspection_version(inspection_id, 1)
                advance_lot_state(self.conn, lot_id, LotState.REVIEWED)
            elif backfilled:
                # 补录:归档到当时生效的版本,保留当时结论,不产生新版本
                target = self._version_in_force(lot_id, occurred)
                self._set_inspection_version(inspection_id, target["version"])
            elif grade.value != current["grade"]:
                # 复检/新一轮抽检推翻前版结论:产生新版本
                new_version = current["version"] + 1
                self.conn.execute(
                    "UPDATE grade_conclusions SET status = 'superseded' WHERE id = ?",
                    (current["id"],),
                )
                old_grade = Grade(current["grade"])
                lead = "复检推翻前版结论" if kind is InspectionKind.REINSPECTION else "新一轮抽检变更结论"
                self._insert_conclusion(
                    lot_id,
                    version=new_version,
                    grade=grade,
                    inspection_id=inspection_id,
                    rule=rule,
                    defect_rate=defect_rate,
                    sample_box_ids=sample_box_ids,
                    supersedes=current["version"],
                    reason=(
                        f"{lead}:{old_grade.value} → {grade.value}"
                        f"(缺陷率 {defect_rate:.2%},规则「{rule['name']}」)"
                    ),
                )
                self._set_inspection_version(inspection_id, new_version)
                # 仅标记仍未交付的受影响订单
                self.orders._mark_affected_locked(lot, new_version, old_grade, grade)
                # 后续不合格:自动召回仍在有效期内的放行
                if grade is Grade.REJECT:
                    self.releases._auto_recall_locked(
                        lot_id, f"批次{lot['code']}复检结论为不合格"
                    )
            else:
                # 结论一致:检验单归档到当前版本,不产生新版本
                self._set_inspection_version(inspection_id, current["version"])
        return self.get(inspection_id)

    # ---------- 查询 ----------
    def get(self, inspection_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM inspections WHERE id = ?", (inspection_id,))
        if row is None:
            raise NotFoundError(f"检验单不存在: {inspection_id}")
        row["sample_box_ids"] = db.loads(row["sample_box_ids"])
        return row

    def list_for_lot(self, lot_id: int) -> list[dict]:
        rows = db.all_rows(
            self.conn, "SELECT * FROM inspections WHERE lot_id = ? ORDER BY id", (lot_id,)
        )
        for row in rows:
            row["sample_box_ids"] = db.loads(row["sample_box_ids"])
        return rows

    def conclusions_for_lot(self, lot_id: int) -> list[dict]:
        rows = db.all_rows(
            self.conn,
            "SELECT * FROM grade_conclusions WHERE lot_id = ? ORDER BY version",
            (lot_id,),
        )
        for row in rows:
            row["basis"] = db.loads(row["basis"])
        return rows

    def current_conclusion(self, lot_id: int) -> dict | None:
        row = db.one(
            self.conn,
            "SELECT * FROM grade_conclusions WHERE lot_id = ? AND status = 'current'",
            (lot_id,),
        )
        if row is not None:
            row["basis"] = db.loads(row["basis"])
        return row

    # ---------- 内部 ----------
    def _insert_conclusion(
        self,
        lot_id,
        version,
        grade,
        inspection_id,
        rule,
        defect_rate,
        sample_box_ids,
        supersedes,
        reason,
    ) -> None:
        basis = {
            "inspection_id": inspection_id,
            "rule_id": rule["id"],
            "rule_name": rule["name"],
            "defect_rate": defect_rate,
            "grade_thresholds": rule["grade_thresholds"],
            "sample_box_ids": list(sample_box_ids),
            "supersedes": supersedes,
        }
        db.insert(
            self.conn,
            "INSERT INTO grade_conclusions(lot_id, version, grade, basis, reason, status,"
            " created_at) VALUES (?, ?, ?, ?, ?, 'current', ?)",
            (
                lot_id,
                version,
                grade.value,
                db.dumps(basis),
                reason,
                db.now_iso(self.clock),
            ),
        )

    def _set_inspection_version(self, inspection_id: int, version: int) -> None:
        self.conn.execute(
            "UPDATE inspections SET conclusion_version = ? WHERE id = ?",
            (version, inspection_id),
        )

    def _version_in_force(self, lot_id: int, occurred_at) -> dict:
        """补录检验发生时刻生效的结论版本;早于首版则归入首版。"""
        rows = db.all_rows(
            self.conn,
            "SELECT * FROM grade_conclusions WHERE lot_id = ? ORDER BY version",
            (lot_id,),
        )
        chosen = None
        for row in rows:
            if db.parse_iso(row["created_at"]) <= occurred_at:
                chosen = row
            else:
                break
        if chosen is None:
            chosen = rows[0]
        return chosen
