"""领域异常。所有业务规则破坏都以 DomainError 子类抛出。"""
from __future__ import annotations


class DomainError(Exception):
    """追溯领域内所有可预期错误的基类。"""


class AuthorizationError(DomainError):
    """角色无权签署对应环节。"""


class ValidationError(DomainError):
    """实体或参数不满足领域约束。"""


class SamplingRuleNotFound(DomainError):
    """按品种、产季、客户合同找不到适用的抽样规则版本。"""


class SamplePlanViolation(DomainError):
    """抽样箱数、样本单位数或分级计数与规则方案不符。"""


class ReleaseBlocked(DomainError):
    """封识不符或证据链缺口阻断放行。

    reasons 中保留每一条阻断原因，调用方应当原样展示或持久化。
    """

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons))


class ConditionalReleaseError(DomainError):
    """有条件放行缺少数量/有效期约束，或数量超出批次余量。"""


class ImmutableHistoryError(DomainError):
    """试图改写已形成的结论；补录与复检只能追加新版本。"""
