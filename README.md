# 进口水果分级追溯

面向商超质量团队的批次分级追溯系统：把**产地装箱 → 集装箱封识 → 到港交接 → 市场抽检 → 客户复检**串成一条连续证据链，支撑等级索赔的举证与裁决。仅依赖 Python 标准库（含 SQLite），无需外部服务。

## 解决的问题

- 只有产地分级表和市场抽检截图，无法证明中间环节属于同一批货；
- 样本事后补录、复检推翻初检、多个检验员同时提交时，结论容易互相覆盖；
- 封识不符、证据缺口时仍然放行了货物；
- 有条件放行无限量、无期限，问题发生后无法召回、无法说清通知了哪些客户。

## 核心规则

| 主题 | 规则实现 |
| --- | --- |
| 连续证据链 | `traceability/chain.py` 逐环节核验：登记、全箱装箱、施封、到港核对、抽样、检验；缺环、时间倒挂、封识不符都列为缺口 |
| 按品种/产季/合同选规则 | `traceability/rules.py`：规则版本只追加，按业务发生时刻选择**已生效且最具体**（合同专用 > 通用）的版本 |
| 角色分权 | `traceability/roles.py`：装箱员、施封人、交接人、市场检验员、客户检验员、质量经理各只能签署自己的环节；审计只读 |
| 补录保真 | 事件只追加（`store.py`），补录保留 `occurred_at` 业务时间与当时结论文本，另记 `recorded_at` |
| 复检新版本 | 复检形成新一轮（round+1）并记录 `supersedes_round`；等级/裁决变化标记改判，只通知**仍未交付**订单 |
| 阻断放行 | 封识不符或证据缺口时 `issue_decision` 直接抛 `ReleaseBlocked`；复检不合格后旧放行不能再提货 |
| 有条件放行 | 必须给出 `qty_limit` 与 `valid_until`；交付受数量与有效期双重约束 |
| 召回 | 有效期届满或后续检验不合格可召回；召回事件追加，旧裁决保留并置为已召回 |
| 并发裁决 | SQLite 部分唯一索引 `(批次, 轮次)`，多名检验员并发提交只可能有一次有效裁决 |
| 箱号审计 | `traceability/audit.py`：从任意箱号查到所属批次、等级为何成立（规则版本/签署人/时间）、结论版本变化、已通知客户 |

## 代码结构

```
traceability/
  events.py    不可变事件与事件类型
  store.py     SQLite 只追加事件存储、唯一约束、用户目录
  state.py     事件重放得到批次/箱/轮次/裁决/订单读模型
  rules.py     抽样规则版本库、规则选择、等级判定
  roles.py     角色与环节绑定
  chain.py     证据链核验
  service.py   领域服务（全部写操作的唯一入口）
  audit.py     箱号审计报告（结构化 + 文本）
tools/
  validate_contract.py   领域资料一致性校验
  demo_traceability.py   端到端演示
domain/contract.json     领域合同（实体/状态/事件/环节角色/策略）
examples/events.json     按时间排列的示例事件
tests/                   unittest 测试（23 个用例）
```

## 构建与测试

```bash
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
python3 tools/validate_contract.py
python3 tools/demo_traceability.py
```

所有命令均在项目根目录执行，不需要启动额外服务。

## 典型用法

```python
from traceability import Store, QualityTraceService, Role, AcceptanceRule, GradeLevel, Verdict
from traceability import build_box_report, render_box_report
from traceability.timeutil import parse

store = Store("trace.db")           # 或 ":memory:"
svc = QualityTraceService(store)
svc.register_user("u-market", "赵工", Role.MARKET_INSPECTOR)
# register_lot → pack_box → record_seal → record_handover
# → collect_sample → submit_inspection → issue_decision ...

report = build_box_report(store, "CL-0004")   # 审计从任意箱号出发
print(render_box_report(report))
```

事件流是唯一事实来源：删除读模型后用 `state.replay(store.all_events())` 可完整复原任一时刻的结论。
