"""连续证据链核验。

一条可放行的证据链必须依次具备：
产地登记 → 全箱装箱 → 集装箱封识（完好）→ 到港交接（封识号核对一致）
→ 按规则抽样（箱号属于本批）→ 检验裁决。
时间先后矛盾、封识不符、任一环节缺失都构成证据缺口。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .state import LotState
from .timeutil import parse


@dataclass
class ChainReport:
    lot_no: str
    clear: bool
    seal_ok: bool
    gaps: list[str] = field(default_factory=list)
    stages: dict[str, dict] = field(default_factory=dict)

    def summary(self) -> str:
        if self.clear:
            return f"批次 {self.lot_no} 证据链完整，封识核对一致"
        return f"批次 {self.lot_no} 证据链存在缺口：" + "；".join(self.gaps)


def verify_lot(lot: LotState) -> ChainReport:
    gaps: list[str] = []
    stages: dict[str, dict] = {}

    # 1. 产地登记与装箱
    if not lot.commodity:
        gaps.append("缺少产地批次登记")
    packed_count = len(lot.boxes)
    stages["pack"] = {"packed_boxes": packed_count, "declared_boxes": lot.declared_boxes}
    if packed_count < lot.declared_boxes:
        gaps.append(
            f"装箱不完整：申报 {lot.declared_boxes} 箱，实际记录 {packed_count} 箱"
        )

    # 2. 封识
    seal_ok = True
    stages["seal"] = {"seal_no": lot.seal_no, "broken": lot.seal_broken}
    if lot.seal_no is None:
        gaps.append("缺少集装箱封识记录")
        seal_ok = False
    elif lot.seal_broken:
        gaps.append(f"封识 {lot.seal_no} 存在破损/异常记录")
        seal_ok = False

    # 3. 到港交接
    if lot.handover is None:
        gaps.append("缺少到港交接记录")
        stages["handover"] = None
    else:
        h = lot.handover
        stages["handover"] = h
        if not h["matched"]:
            gaps.append(
                f"到港封识不符：施封锁号 {h['expected_seal_no']}，到港发现 {h['found_seal_no']}"
            )
            seal_ok = False
        if not h["intact"]:
            gaps.append("到港交接时封识不完整")
            seal_ok = False

    # 4. 时间顺序：装箱 → 封识 → 交接 → 抽样 → 检验
    box_times = [parse(b["packed_at"]) for b in lot.boxes.values()]
    if lot.sealed_at and box_times:
        if parse(lot.sealed_at) < max(box_times):
            gaps.append("时间倒挂：封识时间早于最后一箱装箱时间")
    if lot.handover and lot.sealed_at:
        if parse(lot.handover["at"]) < parse(lot.sealed_at):
            gaps.append("时间倒挂：到港交接早于封识施加")

    # 5. 抽样与检验配套
    for round_no in sorted(lot.samples):
        sample = lot.samples[round_no]
        unknown = [b for b in sample.box_seq if b not in lot.boxes]
        if unknown:
            gaps.append(f"第 {round_no} 轮抽样箱号不属于本批次：{'、'.join(unknown)}")
        if round_no not in lot.inspections:
            gaps.append(f"第 {round_no} 轮有抽样但无检验裁决，证据缺口")
        else:
            sample_at = parse(sample.event.occurred_at)
            inspect_at = parse(lot.inspections[round_no].event.occurred_at)
            if inspect_at < sample_at:
                gaps.append(f"第 {round_no} 轮检验时间早于抽样时间")
        stages.setdefault("rounds", {})[round_no] = {
            "sample_boxes": sample.box_seq,
            "has_inspection": round_no in lot.inspections,
            "rule": f"{sample.rule_id} v{sample.rule_version}",
        }

    if not lot.samples:
        gaps.append("缺少任何抽样记录")

    return ChainReport(
        lot_no=lot.lot_no,
        clear=not gaps,
        seal_ok=seal_ok,
        gaps=gaps,
        stages=stages,
    )
