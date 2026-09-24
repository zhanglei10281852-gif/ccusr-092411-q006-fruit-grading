"""角色与环节：每个环节只允许指定角色签署。"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    ORIGIN_PACKER = "origin_packer"          # 产地装箱员
    CARRIER_SEALER = "carrier_sealer"        # 封识施加人（承运人）
    PORT_RECEIVER = "port_receiver"          # 到港交接人
    MARKET_INSPECTOR = "market_inspector"    # 市场抽检员
    CUSTOMER_INSPECTOR = "customer_inspector"  # 客户复检员
    QUALITY_MANAGER = "quality_manager"      # 质量放行裁决人
    AUDITOR = "auditor"                      # 审计人员（只读）


class Stage(str, Enum):
    PACK = "pack"                    # 产地装箱
    SEAL = "seal"                    # 集装箱封识
    HANDOVER = "handover"            # 到港交接
    MARKET_SAMPLE = "market_sample"  # 市场抽检抽样
    MARKET_INSPECTION = "market_inspection"
    CUSTOMER_SAMPLE = "customer_sample"    # 客户复检抽样
    CUSTOMER_INSPECTION = "customer_inspection"
    RELEASE = "release"              # 放行裁决


# 环节 → 唯一有权签署的角色
STAGE_ROLE: dict[Stage, Role] = {
    Stage.PACK: Role.ORIGIN_PACKER,
    Stage.SEAL: Role.CARRIER_SEALER,
    Stage.HANDOVER: Role.PORT_RECEIVER,
    Stage.MARKET_SAMPLE: Role.MARKET_INSPECTOR,
    Stage.MARKET_INSPECTION: Role.MARKET_INSPECTOR,
    Stage.CUSTOMER_SAMPLE: Role.CUSTOMER_INSPECTOR,
    Stage.CUSTOMER_INSPECTION: Role.CUSTOMER_INSPECTOR,
    Stage.RELEASE: Role.QUALITY_MANAGER,
}

ROLE_LABEL = {
    Role.ORIGIN_PACKER: "产地装箱员",
    Role.CARRIER_SEALER: "封识施加人",
    Role.PORT_RECEIVER: "到港交接人",
    Role.MARKET_INSPECTOR: "市场抽检员",
    Role.CUSTOMER_INSPECTOR: "客户复检员",
    Role.QUALITY_MANAGER: "质量经理",
    Role.AUDITOR: "审计员",
}

STAGE_LABEL = {
    Stage.PACK: "产地装箱",
    Stage.SEAL: "集装箱封识",
    Stage.HANDOVER: "到港交接",
    Stage.MARKET_SAMPLE: "市场抽检抽样",
    Stage.MARKET_INSPECTION: "市场初检裁决",
    Stage.CUSTOMER_SAMPLE: "客户复检抽样",
    Stage.CUSTOMER_INSPECTION: "客户复检裁决",
    Stage.RELEASE: "放行裁决",
}
