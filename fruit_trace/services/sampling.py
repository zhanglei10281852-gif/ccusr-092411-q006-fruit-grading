"""抽样规则选用与等级评定。

规则按 合同专属 > 品种+产季 > 品种 > 产季 > 默认 的优先级选用;
缺陷率不超过某等级阈值即判该等级,全部超出则判不合格(REJECT)。
"""
from __future__ import annotations

import math

from .. import db
from ..constants import Grade
from ..errors import NotFoundError, ValidationError


class SamplingService:
    def __init__(self, conn):
        self.conn = conn

    def select_rule(self, variety: str, season: str, contract_id: int | None = None) -> dict:
        """按品种、产季与客户合同选用最具体的适用规则。"""
        best = None
        best_key = None
        for rule in db.all_rows(self.conn, "SELECT * FROM sampling_rules"):
            if rule["contract_id"] is not None and rule["contract_id"] != contract_id:
                continue
            if rule["variety"] is not None and rule["variety"] != variety:
                continue
            if rule["season"] is not None and rule["season"] != season:
                continue
            score = (
                (rule["contract_id"] is not None) * 4
                + (rule["variety"] is not None) * 2
                + (rule["season"] is not None) * 1
            )
            key = (score, rule["priority"], -rule["id"])
            if best_key is None or key > best_key:
                best, best_key = rule, key
        if best is None:
            raise NotFoundError(
                f"未找到适用的抽样规则(品种={variety},产季={season},合同={contract_id})"
            )
        best["grade_thresholds"] = db.loads(best["grade_thresholds"])
        return best

    @staticmethod
    def sample_size(rule: dict, total_boxes: int) -> int:
        """按规则计算抽样箱数:比例换算后夹在 [min_sample, max_sample] 之间。"""
        if total_boxes <= 0:
            raise ValidationError("批次箱数必须为正")
        n = math.ceil(total_boxes * rule["sample_ratio"])
        n = max(n, rule["min_sample"])
        n = min(n, rule["max_sample"])
        return min(n, total_boxes)

    @staticmethod
    def evaluate_grade(rule: dict, defect_rate: float) -> Grade:
        """缺陷率不超过阈值即判对应等级,否则判不合格。"""
        thresholds = rule["grade_thresholds"]
        if isinstance(thresholds, str):
            thresholds = db.loads(thresholds)
        for grade in (Grade.A, Grade.B, Grade.C):
            if defect_rate <= thresholds[grade.value]:
                return grade
        return Grade.REJECT
