# 进口水果分级追溯

面向商超质量团队的批次分级追溯系统:产地装箱、集装箱封识、到港交接、市场抽检
与客户复检组成连续证据链,等级结论全程版本化,放行可限量限期并召回,
审计人员可从任意箱号还原"等级为何成立、结论怎样变化、哪些客户已收到通知"。

纯 Python 标准库实现(SQLite 持久化),无需安装依赖。

## 目录结构

- `fruit_trace/` — 业务服务包
  - `constants.py` — 角色、环节、等级、状态及其映射(与 `domain/contract.json` 对齐)
  - `db.py` — SQLite 连接、建表与可重入事务;唯一约束承载关键不变量
  - `lots.py` — 批次状态机(只能前进)
  - `services/` — 领域服务
    - `registry.py` — 用户、客户合同、抽样规则、批次与箱单登记
    - `evidence.py` — 证据链签署与核验(链式引用、角色校验、封识比对)
    - `sampling.py` — 抽样规则选用(合同 > 品种+产季 > 品种 > 默认)与等级评定
    - `inspection.py` — 检验记录、补录归档、结论版本化、受影响订单标记
    - `ruling.py` — 放行裁决(并发唯一、幂等重试)
    - `release.py` — 放行/交付/到期/召回
    - `orders.py` — 客户订单与通知
    - `audit.py` — 箱号审计与报告渲染
  - `system.py` — `TraceSystem` 门面,组装全部服务
  - `testing.py` — 演示与测试共用的数据构造器
- `domain/contract.json` — 实体、状态、事件类型和时间约定
- `examples/events.json` — 按发生时间排列的示例事件
- `examples/demo.py` — 端到端演示(索赔处置全流程 + 反例)
- `tools/validate_contract.py` — 领域资料一致性校验
- `tests/` — 单元测试(49 个用例)

## 核心业务规则

| 规则 | 实现 |
| --- | --- |
| 证据链连续 | 后一环节必须引用前一环节证据;每环节仅由对应角色签署一次 |
| 抽样规则选用 | 合同专属 > 品种+产季 > 品种 > 产季 > 默认,缺陷率对照阈值定级 |
| 样本补录 | 发生时间早于当前结论的检验归档到当时版本,保留当时结论 |
| 复检推翻初检 | 产生新版本结论,旧版作废;仅标记仍未交付的订单并通知客户 |
| 角色约束 | 环节签署、检验提交、裁决放行分别限定角色,越权即拒绝 |
| 放行阻断 | 证据缺口、封识不符、结论不合格、裁决不允许 → 全部原因一次返回 |
| 有条件放行 | 必须限定数量与有效期;交付累计限量;期满或后续不合格可召回 |
| 裁决唯一 | `(批次, 轮次)` 唯一约束,并发提交只有第一个生效;幂等键防重 |
| 箱号审计 | 等级依据(规则/缺陷率/样本)、结论变迁、客户通知清单一并输出 |

## 构建

```bash
python3 -m compileall -q .
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 资料校验

```bash
python3 tools/validate_contract.py
```

## 运行演示

```bash
python3 examples/demo.py
```

演示覆盖:证据链签署 → 初检定级 → 有条件放行与限量交付 → 样本补录 →
复检推翻并自动召回 → 并发裁决 → 箱号审计 → 封识不符/证据缺口阻断放行。

## 快速上手

```python
from fruit_trace import TraceSystem, testing

system = TraceSystem("trace.db")          # 或 ":memory:"
ids = testing.seed_base(system)           # 示例主数据(实际部署用 registry 逐项登记)

testing.run_evidence_chain(system, ids)   # 装箱→封识→交接→抽检
testing.run_initial_inspection(system, ids)  # 初检 → 结论 v1

ruling = system.rulings.submit(ids["lot_id"], 1, "conditional", ids["qa"], "rk-1")
release = system.releases.request_release(
    ids["lot_id"], ruling["id"], "conditional",
    max_quantity=400, valid_until=<带时区的截止时间>, actor_id=ids["qa"],
)

report = system.audit.audit_box("LOT-2026-0001-CNTR-01-0001")
```

所有命令均在项目根目录执行,不需要启动额外服务。
