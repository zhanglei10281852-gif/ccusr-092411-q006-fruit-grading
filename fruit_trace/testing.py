"""演示与测试共用的数据构造器。

seed_base 搭好一套典型主数据:七个角色、一个客户合同、三级抽样规则
(默认 / 品种+产季 / 合同专属)、一个两柜批次;run_evidence_chain 与
run_initial_inspection 把批次推进到"已有初检结论"的状态。
"""
from __future__ import annotations

from .constants import Grade, Stage

VARIETY = "车厘子"
SEASON = "2026年春季"
LOT_CODE = "LOT-2026-0001"
CONTAINERS = {"CNTR-01": 12, "CNTR-02": 12}


def seed_users(system) -> dict:
    registry = system.registry
    return {
        "packer": registry.register_user("张装箱", "origin_packer")["id"],
        "sealer": registry.register_user("李封识", "origin_sealer")["id"],
        "receiver": registry.register_user("王交接", "port_receiver")["id"],
        "inspector": registry.register_user("赵抽检", "market_inspector")["id"],
        "reinspector": registry.register_user("钱复检", "customer_inspector")["id"],
        "qa": registry.register_user("孙质管", "qa_manager")["id"],
        "auditor": registry.register_user("周审计", "auditor")["id"],
    }


def seed_rules(system, contract_id: int) -> dict:
    registry = system.registry
    return {
        "default": registry.register_sampling_rule(
            "默认规则",
            sample_ratio=0.05,
            min_sample=5,
            max_sample=50,
            grade_thresholds={"A": 0.02, "B": 0.05, "C": 0.10},
        )["id"],
        "variety_season": registry.register_sampling_rule(
            "车厘子2026春季规则",
            variety=VARIETY,
            season=SEASON,
            sample_ratio=0.10,
            min_sample=8,
            max_sample=60,
            grade_thresholds={"A": 0.02, "B": 0.04, "C": 0.08},
        )["id"],
        "contract": registry.register_sampling_rule(
            "鲜果汇合同专属规则",
            contract_id=contract_id,
            sample_ratio=0.20,
            min_sample=10,
            max_sample=80,
            grade_thresholds={"A": 0.01, "B": 0.03, "C": 0.06},
        )["id"],
    }


def seed_base(system, containers: dict | None = None) -> dict:
    """构造主数据与批次,返回各环节操作所需的标识。"""
    containers = containers or dict(CONTAINERS)
    registry = system.registry
    users = seed_users(system)
    contract = registry.register_contract("鲜果汇超市", VARIETY, SEASON, Grade.A)
    rules = seed_rules(system, contract["id"])
    lot = registry.register_lot(LOT_CODE, VARIETY, SEASON, containers, contract_id=contract["id"])
    boxes = registry.lot_boxes(lot["id"])
    by_container: dict[str, list[str]] = {}
    for box in boxes:
        container_no = box["box_no"].rsplit("-", 1)[0].replace(f"{LOT_CODE}-", "")
        by_container.setdefault(container_no, []).append(box["box_no"])
    return {
        **users,
        "contract_id": contract["id"],
        "rules": rules,
        "lot_id": lot["id"],
        "lot_code": lot["code"],
        "containers": by_container,
        "box_nos": [b["box_no"] for b in boxes],
    }


def packing_payload(ids: dict) -> dict:
    return {
        "origin": "智利中央山谷",
        "containers": [
            {"container_no": no, "box_count": len(boxes)}
            for no, boxes in ids["containers"].items()
        ],
    }


def sealing_payload(ids: dict) -> dict:
    return {
        "seals": [
            {"container_no": no, "seal_no": f"SEAL-{no}"} for no in ids["containers"]
        ]
    }


def handover_payload(ids: dict, *, intact: bool = True, tamper_seal: bool = False) -> dict:
    checks = []
    for no in ids["containers"]:
        seal_no = f"SEAL-{no}"
        if tamper_seal and no == sorted(ids["containers"])[0]:
            seal_no = "SEAL-UNKNOWN"  # 到港核验发现封识号与产地记录不符
        checks.append({"container_no": no, "seal_no": seal_no, "intact": intact})
    return {"port": "上海洋山港", "checks": checks}


def sample_boxes(ids: dict, count: int = 10) -> list[str]:
    return ids["box_nos"][:count]


def run_evidence_chain(system, ids: dict, *, intact: bool = True, tamper_seal: bool = False) -> None:
    """依次签署 装箱 → 封识 → 到港交接 → 市场抽检 四个环节。"""
    evidence = system.evidence
    lot_id = ids["lot_id"]
    evidence.record_event(lot_id, Stage.PACKING, ids["packer"], packing_payload(ids))
    evidence.record_event(lot_id, Stage.SEALING, ids["sealer"], sealing_payload(ids))
    evidence.record_event(
        lot_id,
        Stage.PORT_HANDOVER,
        ids["receiver"],
        handover_payload(ids, intact=intact, tamper_seal=tamper_seal),
    )
    evidence.record_event(
        lot_id,
        Stage.MARKET_SAMPLING,
        ids["inspector"],
        {"location": "上海农产品中心批发市场", "sample_box_ids": sample_boxes(ids)},
    )


def run_initial_inspection(system, ids: dict, *, total: int = 1000, defects: int = 5, occurred_at=None):
    """提交初检(默认缺陷率 0.5%,按合同专属规则判 A 级)。"""
    return system.inspections.record_inspection(
        ids["lot_id"],
        "initial",
        ids["inspector"],
        sample_boxes(ids),
        total,
        defects,
        occurred_at=occurred_at,
    )


def run_reinspection(system, ids: dict, *, total: int = 1000, defects: int = 120, occurred_at=None):
    """客户复检:先登记复检环节证据,再提交复检检验单。"""
    system.evidence.record_event(
        ids["lot_id"],
        Stage.CUSTOMER_REINSPECTION,
        ids["reinspector"],
        {"customer": "鲜果汇超市", "sample_box_ids": sample_boxes(ids, 6)},
    )
    return system.inspections.record_inspection(
        ids["lot_id"],
        "reinspection",
        ids["reinspector"],
        sample_boxes(ids, 6),
        total,
        defects,
        occurred_at=occurred_at,
    )
