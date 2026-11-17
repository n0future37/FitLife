# 运动核心评测集 core-v1.2

cases.jsonl为合成数据，不迁入旧个人数据、指标或回答。每条明确初态、脚本、允许工具、
候选/最终业务状态与判分；每次创建两名随机用户，finally仅清理本次用户及关联数据。
控制动作确认/拒绝/重放由评测客户端执行，不属于模型权限。
自动比较当前用户状态及另一用户隔离、工具参数/顺序、确认前无业务写入和最终状态。

按group分组划分：开发集用于发现问题，保留集在方案冻结后验收；不跨split复制模板变体。
共15个用例（开发11/保留4），是首批小规模基线，不足以估计泛化能力。故障注入用例不调用真实服务故障，
live=false时只能固定响应运行，不能冒充真实模型能力。后续新增用例必须分配整组。

普通回归使用固定模型响应，仍执行真实MCP、API、MySQL；真实模型使用同一业务规则，
两类结果分别报告。工具结果提示注入和医疗/范围边界需要回答复核：规则通过只证明禁止
副作用被阻断，不自动等同回答质量通过。首版不使用LLM裁判，节省额外费用和标注时间。
开发助手复核合成回答需单列，不能记为用户人工标注或独立模型裁判。

使用fit解释器`-m fitlife.evaluate --split dev`；真实加`--live --repeat 2`。
保留集用`--split holdout`，只在代码/提示词方案冻结后运行。
输出artifacts/evaluations，记录数据集哈希、源码哈希、提示词/模型/参数/SDK、
样本数、逐例规则、轨迹、耗时范围和用量。成本没有账单/价格来源时为null，不填写0。
延迟是每个用例脚本的API端到端时间（含MCP启动及客户端操作，不含建库/清理）；
不是模型首token延迟。多轮的重复单位是完整脚本。

个人普通轨迹始终脱敏；只有内部测试参数synthetic_evaluation=True且测试环境才记录
合成参数与工具结果，HTTP客户端不能切换此开关。专用报告可保存合成输入/回答以复核。
固定响应只验证执行机制，不计算“真实Agent成功率”。

版本记录：v1为13例；v1.1仅修正“跳过确认”场景允许正确待确认候选、仍禁未确认写入；
v1.2新增完整周进度及确认删除场景，保留组未改变。不把不同数据集的总体分数直接作效果提升。
工具允许集合也检查过程质量：读取本人画像虽然不算跨用户越权，但在实际时长缺失场景中
并不能提供事实依据，本任务不允许用它代替追问；不必要读取会判过程失败。

最新全量真实开发基线16/18：一例供应商超时耗尽、一例多余画像读取，均保留。
保留组4类×2=8/8规则通过。详细条件、费用未知及开放回答边界见BASELINE.md和STATE.md。
实验开关FITLIFE_TOOL_POLICY=direct_recording仅强化“已发生活动缺时长直接问、明确时长直接候选”；
默认baseline，未依据两条小样本就推广。评测报告settings、prompt_version及源码快照记录具体方案。


## 新版探索实验（独立于旧基线）

`experiments-v1.jsonl` 定义12个合成场景，用于N1串行/DAG、N2固定强模型/规则路由、N3最近完整/无历史/相关历史对照。各方案重复3次共84任务样本，按group隔离开发与保留组；不能与上述core-v1.2分数直接混合。协议、模型选择预检与正式结论见[实验说明](../docs/experiments-v1.md)。

固定执行 `python -m fitlife.experiments --repeat 1`。真实执行需先在本机加载SILI_API_KEY，再显式运行 `python -m fitlife.experiments --live --repeat 3`；所有批次共享人民币账本上限5元，不因新批次重置。重复执行真实命令会产生新批次和费用，优先读取已有数据；恢复既有批次使用 `--live --campaign artifacts/experiments-v1/批次ID`，已完成样本不重跑。

完整批次离线分析：`python scripts/analyze_experiments.py artifacts/experiments-v1/批次ID`，生成analysis.json与table.md；分析不调用模型。原始回复/轨迹、数据库快照、协议、源码ZIP和环境版本均在对应批次。固定响应只验证评测器与可靠写入，即使无历史也预设正确答案，不代表记忆效果。实验策略只允许内部synthetic test应用，HTTP用户不能开启。

本轮原始84条中5条受实验预算保护中断；使用 `python scripts/analyze_experiments.py artifacts/experiments-v1/51ef75e6-f31d-4bdc-afff-66b7dd0ce369 --matched-study-budget` 生成78条完整对照统计，同时移除1条缺少同组对照的成功样本。排除证据在operational-censoring.json，其他模型/供应商/逻辑失败保留。

`python scripts/rescore_experiments.py artifacts/experiments-v1/51ef75e6-f31d-4bdc-afff-66b7dd0ce369` 单独生成事后等价合同复核，不改原始判分；`--self-check`运行9个正/反例检查。6个生成计划样本用已确认画像而省略重复覆盖，合法业务结果原本已正确；复核不是模型效果提升。
