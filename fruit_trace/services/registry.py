"""主数据登记:用户、客户合同、抽样规则、批次与箱单。"""
from __future__ import annotations

from .. import db
from ..constants import Grade, Role
from ..errors import NotFoundError, ValidationError


class RegistryService:
    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock

    # ---------- 用户 ----------
    def register_user(self, name: str, role) -> dict:
        role = Role(role)
        with db.tx(self.conn):
            user_id = db.insert(
                self.conn, "INSERT INTO users(name, role) VALUES (?, ?)", (name, role.value)
            )
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(f"用户不存在: {user_id}")
        return row

    # ---------- 客户合同 ----------
    def register_contract(
        self, customer_name: str, variety: str, season: str, grade_required, terms: str = ""
    ) -> dict:
        grade = Grade(grade_required)
        if grade is Grade.REJECT:
            raise ValidationError("合同要求等级不能为不合格")
        with db.tx(self.conn):
            contract_id = db.insert(
                self.conn,
                "INSERT INTO contracts(customer_name, variety, season, grade_required, terms)"
                " VALUES (?, ?, ?, ?, ?)",
                (customer_name, variety, season, grade.value, terms),
            )
        return self.get_contract(contract_id)

    def get_contract(self, contract_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM contracts WHERE id = ?", (contract_id,))
        if row is None:
            raise NotFoundError(f"合同不存在: {contract_id}")
        return row

    # ---------- 抽样规则 ----------
    def register_sampling_rule(
        self,
        name: str,
        sample_ratio: float,
        min_sample: int,
        max_sample: int,
        grade_thresholds: dict,
        variety: str | None = None,
        season: str | None = None,
        contract_id: int | None = None,
        priority: int = 0,
    ) -> dict:
        """登记抽样规则。variety/season/contract_id 为 NULL 表示该维度不限,
        选用时按 合同 > 品种+产季 > 品种 > 产季 > 默认 的优先级匹配。"""
        if not 0 < sample_ratio <= 1:
            raise ValidationError("抽样比例必须在 (0, 1] 之间")
        if not 1 <= min_sample <= max_sample:
            raise ValidationError("抽样箱数须满足 1 <= min_sample <= max_sample")
        expected = {Grade.A.value, Grade.B.value, Grade.C.value}
        if set(grade_thresholds) != expected:
            raise ValidationError(f"等级阈值必须恰好包含 {sorted(expected)}")
        ladder = [grade_thresholds[g] for g in (Grade.A.value, Grade.B.value, Grade.C.value)]
        if any(not 0 <= t <= 1 for t in ladder) or ladder != sorted(ladder):
            raise ValidationError("等级阈值必须在 [0,1] 内且按 A<=B<=C 递增")
        if contract_id is not None:
            self.get_contract(contract_id)
        with db.tx(self.conn):
            rule_id = db.insert(
                self.conn,
                "INSERT INTO sampling_rules(name, variety, season, contract_id, sample_ratio,"
                " min_sample, max_sample, grade_thresholds, priority)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    variety,
                    season,
                    contract_id,
                    sample_ratio,
                    min_sample,
                    max_sample,
                    db.dumps(grade_thresholds),
                    priority,
                ),
            )
        return self.get_sampling_rule(rule_id)

    def get_sampling_rule(self, rule_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM sampling_rules WHERE id = ?", (rule_id,))
        if row is None:
            raise NotFoundError(f"抽样规则不存在: {rule_id}")
        row["grade_thresholds"] = db.loads(row["grade_thresholds"])
        return row

    # ---------- 批次与箱单 ----------
    def register_lot(
        self,
        code: str,
        variety: str,
        season: str,
        containers: dict,
        contract_id: int | None = None,
    ) -> dict:
        """登记批次并生成箱单。containers: {集装箱号: 箱数}。"""
        if not containers:
            raise ValidationError("批次至少包含一个集装箱")
        if contract_id is not None:
            self.get_contract(contract_id)
        with db.tx(self.conn):
            lot_id = db.insert(
                self.conn,
                "INSERT INTO lots(code, variety, season, contract_id, state, created_at)"
                " VALUES (?, ?, ?, ?, 'registered', ?)",
                (code, variety, season, contract_id, db.now_iso(self.clock)),
            )
            for container_no, box_count in containers.items():
                if not isinstance(box_count, int) or box_count <= 0:
                    raise ValidationError(f"集装箱 {container_no} 的箱数必须为正整数")
                container_id = db.insert(
                    self.conn,
                    "INSERT INTO containers(lot_id, container_no) VALUES (?, ?)",
                    (lot_id, container_no),
                )
                for i in range(1, box_count + 1):
                    db.insert(
                        self.conn,
                        "INSERT INTO boxes(lot_id, container_id, box_no) VALUES (?, ?, ?)",
                        (lot_id, container_id, f"{code}-{container_no}-{i:04d}"),
                    )
        return self.get_lot(lot_id)

    def get_lot(self, lot_id: int) -> dict:
        row = db.one(self.conn, "SELECT * FROM lots WHERE id = ?", (lot_id,))
        if row is None:
            raise NotFoundError(f"批次不存在: {lot_id}")
        return row

    def get_lot_by_code(self, code: str) -> dict:
        row = db.one(self.conn, "SELECT * FROM lots WHERE code = ?", (code,))
        if row is None:
            raise NotFoundError(f"批次不存在: {code}")
        return row

    def lot_containers(self, lot_id: int) -> list[dict]:
        return db.all_rows(
            self.conn, "SELECT * FROM containers WHERE lot_id = ? ORDER BY container_no", (lot_id,)
        )

    def lot_boxes(self, lot_id: int) -> list[dict]:
        return db.all_rows(
            self.conn, "SELECT * FROM boxes WHERE lot_id = ? ORDER BY box_no", (lot_id,)
        )
