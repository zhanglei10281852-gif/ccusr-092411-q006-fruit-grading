"""领域常量:角色、环节、等级、状态及其映射关系。

与 domain/contract.json 中的实体、状态、事件类型保持一致。
"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """系统角色。每个证据环节、每类检验都绑定唯一可签署角色。"""

    ORIGIN_PACKER = "origin_packer"  # 产地装箱员
    ORIGIN_SEALER = "origin_sealer"  # 产地封识员
    PORT_RECEIVER = "port_receiver"  # 到港交接员
    MARKET_INSPECTOR = "market_inspector"  # 市场抽检员
    CUSTOMER_INSPECTOR = "customer_inspector"  # 客户复检员
    QA_MANAGER = "qa_manager"  # 质量经理(裁决与放行)
    AUDITOR = "auditor"  # 审计(只读,不能签署任何环节)


class Stage(str, Enum):
    """证据链环节,按物流顺序排列。"""

    PACKING = "packing"  # 产地装箱
    SEALING = "sealing"  # 集装箱封识
    PORT_HANDOVER = "port_handover"  # 到港交接
    MARKET_SAMPLING = "market_sampling"  # 市场抽检
    CUSTOMER_REINSPECTION = "customer_reinspection"  # 客户复检


#: 每个环节只能由对应角色签署
STAGE_ROLE: dict[Stage, Role] = {
    Stage.PACKING: Role.ORIGIN_PACKER,
    Stage.SEALING: Role.ORIGIN_SEALER,
    Stage.PORT_HANDOVER: Role.PORT_RECEIVER,
    Stage.MARKET_SAMPLING: Role.MARKET_INSPECTOR,
    Stage.CUSTOMER_REINSPECTION: Role.CUSTOMER_INSPECTOR,
}

#: 环节顺序,后一环节的证据必须引用前一环节
STAGE_ORDER: list[Stage] = [
    Stage.PACKING,
    Stage.SEALING,
    Stage.PORT_HANDOVER,
    Stage.MARKET_SAMPLING,
    Stage.CUSTOMER_REINSPECTION,
]

#: 放行前必须闭合的环节(客户复检发生在交付之后,不阻断放行)
RELEASE_REQUIRED_STAGES: list[Stage] = [
    Stage.PACKING,
    Stage.SEALING,
    Stage.PORT_HANDOVER,
    Stage.MARKET_SAMPLING,
]


class Grade(str, Enum):
    """等级结论。"""

    A = "A"  # 特级
    B = "B"  # 一级
    C = "C"  # 二级
    REJECT = "REJECT"  # 等外/不合格


#: 等级高低排序,用于判断结论是否降级
GRADE_RANK: dict[Grade, int] = {Grade.A: 3, Grade.B: 2, Grade.C: 1, Grade.REJECT: 0}


class InspectionKind(str, Enum):
    INITIAL = "initial"  # 初检(市场抽检)
    REINSPECTION = "reinspection"  # 客户复检


#: 每类检验只能由对应角色提交
INSPECTION_ROLE: dict[InspectionKind, Role] = {
    InspectionKind.INITIAL: Role.MARKET_INSPECTOR,
    InspectionKind.REINSPECTION: Role.CUSTOMER_INSPECTOR,
}

#: 每类检验必须挂在对应环节的证据上,样本箱须来自该环节登记的抽样箱
INSPECTION_STAGE: dict[InspectionKind, Stage] = {
    InspectionKind.INITIAL: Stage.MARKET_SAMPLING,
    InspectionKind.REINSPECTION: Stage.CUSTOMER_REINSPECTION,
}


class LotState(str, Enum):
    """批次状态,对应 domain/contract.json 的 states,只能前进不能回退。"""

    REGISTERED = "registered"  # 已登记,尚未装箱
    PACKED = "packed"
    SEALED = "sealed"
    ARRIVED = "arrived"
    SAMPLING = "sampling"
    REVIEWED = "reviewed"
    RELEASED = "released"
    CONDITIONAL = "conditional"
    RECALLED = "recalled"


#: 批次状态的前进顺序
LOT_STATE_ORDER: list[LotState] = [
    LotState.REGISTERED,
    LotState.PACKED,
    LotState.SEALED,
    LotState.ARRIVED,
    LotState.SAMPLING,
    LotState.REVIEWED,
    LotState.RELEASED,
    LotState.CONDITIONAL,
    LotState.RECALLED,
]

#: 证据环节签署后批次状态的迁移
STAGE_TO_LOT_STATE: dict[Stage, LotState] = {
    Stage.PACKING: LotState.PACKED,
    Stage.SEALING: LotState.SEALED,
    Stage.PORT_HANDOVER: LotState.ARRIVED,
    Stage.MARKET_SAMPLING: LotState.SAMPLING,
    Stage.CUSTOMER_REINSPECTION: LotState.REVIEWED,
}


class OrderStatus(str, Enum):
    PENDING = "pending"  # 待交付
    DELIVERED = "delivered"  # 已交付
    AFFECTED = "affected"  # 受结论变更影响,暂停交付
    RECALLED = "recalled"  # 已召回
    CANCELLED = "cancelled"  # 已取消


class RulingOutcome(str, Enum):
    PASS = "pass"  # 合格,可完全放行
    CONDITIONAL = "conditional"  # 有条件放行
    REJECT = "reject"  # 不予放行


class ReleaseKind(str, Enum):
    FULL = "full"  # 完全放行
    CONDITIONAL = "conditional"  # 有条件放行(必须限定数量与有效期)


class ReleaseStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"  # 已过有效期
    RECALLED = "recalled"  # 已召回


class NotificationKind(str, Enum):
    RELEASE_GRANTED = "release_granted"  # 放行通知
    ORDER_AFFECTED = "order_affected"  # 订单受影响通知
    RECALL = "recall"  # 召回通知
