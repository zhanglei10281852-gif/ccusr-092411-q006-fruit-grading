"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务错误的基类。"""


class NotFoundError(DomainError):
    """实体不存在。"""


class PermissionDeniedError(DomainError):
    """角色无权执行该操作(例如签署不属于自己的环节)。"""


class EvidenceGapError(DomainError):
    """证据链存在缺口:前置环节缺失,当前环节无法签署或检验无法立案。"""


class ReleaseBlockedError(DomainError):
    """放行被阻断:证据缺口、封识不符或结论不合格。reasons 列出全部阻断原因。"""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons))


class RulingConflictError(DomainError):
    """同一批次同一轮次已存在有效裁决,并发提交中只有第一个被接受。"""


class ValidationError(DomainError):
    """输入不满足业务约束。"""
