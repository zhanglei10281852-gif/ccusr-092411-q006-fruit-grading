"""抽样与分级判定规则。

规则按 品种 × 产季 × 客户合同 选择；同一适用键可以发布多个版本，
生效后不可修改，只能追加新版本（contract.json 中的 version_policy）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from .errors import SamplingRuleNotFound
from .timeutil import parse


class Verdict(str, Enum):
    QUALIFIED = "qualified"        # 合格
    CONDITIONAL = "conditional"    # 降级有条件接收
    REJECTED = "rejected"          # 不合格


@dataclass(frozen=True)
class GradeLevel:
    """一个等级允许的不合格率上限，以及落入该等级时给出的裁决。"""

    name: str
    max_defect_rate: float          # 不合格品率上限（含），0~1
    verdict: Verdict


@dataclass(frozen=True)
class AcceptanceRule:
    """一个不可变的抽样规则版本（简化 AQL 风格）。

    min_boxes/max_boxes：应抽箱数区间；min_units：每箱累计最少样本单位数。
    抽样记录的箱数与单位数必须落在方案内，检验才可被签署。
    """

    rule_id: str
    version: int
    commodity: str                  # 品种，"*" 表示任意品种
    season: str                     # 产季，"*" 表示任意产季
    contract_no: str               # 客户合同号，"*" 表示通用规则
    effective_from: datetime
    levels: tuple[GradeLevel, ...]
    min_boxes: int
    max_boxes: int
    min_units: int
    supersedes_version: int | None = None
    published_at: datetime | None = None
    note: str = ""

    def applies_to(self, commodity: str, season: str, contract_no: str) -> bool:
        return (
            (self.commodity in ("*", commodity))
            and (self.season in ("*", season))
            and (self.contract_no in ("*", contract_no))
        )

    def key(self) -> tuple[str, str, str]:
        return (self.commodity, self.season, self.contract_no)


@dataclass
class RuleRegistry:
    """只追加的规则版本库。发布后的规则冻结，不允许覆盖同版本。"""

    _rules: dict[str, list[AcceptanceRule]] = field(default_factory=dict)

    def publish(self, rule: AcceptanceRule) -> None:
        versions = self._rules.setdefault(rule.rule_id, [])
        if any(r.version == rule.version for r in versions):
            raise ValueError(f"规则 {rule.rule_id} 版本 v{rule.version} 已发布，不可修改")
        versions.append(rule)
        versions.sort(key=lambda r: r.version)

    def versions(self, rule_id: str) -> tuple[AcceptanceRule, ...]:
        return tuple(self._rules.get(rule_id, ()))

    def select(
        self,
        commodity: str,
        season: str,
        contract_no: str,
        at: str | datetime,
    ) -> AcceptanceRule:
        """选择在 at 时刻已生效、且匹配最具体的最新版本。

        具体度：完全指定合同 > 通用合同；指定品种/产季 > 通配。
        同具体度时取版本号最高者。
        """
        moment = parse(at)
        candidates = [
            rule
            for versions in self._rules.values()
            for rule in versions
            if rule.applies_to(commodity, season, contract_no)
            and rule.effective_from <= moment
        ]
        if not candidates:
            raise SamplingRuleNotFound(
                f"无适用抽样规则：品种={commodity} 产季={season} 合同={contract_no}"
            )

        def specificity(rule: AcceptanceRule) -> tuple[int, int, int, int]:
            return (
                0 if rule.contract_no == contract_no else 1,
                0 if rule.commodity == commodity else 1,
                0 if rule.season == season else 1,
                -rule.version,
            )

        return min(candidates, key=specificity)


def evaluate_grade(rule: AcceptanceRule, defective_units: int, total_units: int) -> tuple[str, Verdict, float]:
    """按规则等级表判定等级与裁决。返回 (等级名, 裁决, 实际不合格率)。"""
    if total_units <= 0:
        raise ValueError("样本单位数必须大于 0")
    rate = defective_units / total_units
    chosen: GradeLevel | None = None
    for level in sorted(rule.levels, key=lambda lv: lv.max_defect_rate):
        if rate <= level.max_defect_rate:
            chosen = level
            break
    if chosen is None:
        # 超出所有等级上限：取最宽松等级名但裁决为拒收
        loosest = max(rule.levels, key=lambda lv: lv.max_defect_rate)
        return loosest.name, Verdict.REJECTED, rate
    return chosen.name, chosen.verdict, rate
