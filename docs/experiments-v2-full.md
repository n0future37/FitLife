# FitLife 修复后完整多设置实模复测报告

日期：2026-11-03。批次：`15832c96-6ad3-4c2b-9a29-163354decc21`。修复版本intent-bind-v2.2。
本轮全新运行12个已见合成场景，每方案每场景3次，共84个预定位置；所有成功、失败、超时均保留，未挑选最好结果替换。本批没有追加新场景，不作为新的独立保留测试集。旧90次探索运行及修复验收记录保持原样。

## 0. 本轮观察

- 84个位置中68项完整合同通过、16项失败。业务结果与完整合同在本批通过数相同；各设置分母各12，不把混合配置总通过比例当作默认Agent在真实用户中的成功率。
- N1两组均10/12。9对双方成功任务的均值为串行67.169秒、DAG56.006秒，本批减少16.62%；其中只有8对用量完整，Token129225→129400，费用估算0.252363→0.252514元，未见Token/费用收益。不能把旧实验仅3对并列查询的47.38%直接当作本轮总体收益。
- N2固定强模型8/12、rules路由10/12，但8对双方成功任务均值34.774→48.338秒，路由组更慢。只有简单查询实际切换8B，其余三种任务两组均为同一Pro/DeepSeek模型，因此补问通过数差异不能直接归因于切换模型。
- 简单查询两组各3/3；固定11.606±0.493秒，路由18.563±15.162秒。路由第三次8B调用30秒超时，重试后完成，总计40.003秒。3次已知费用估算0.021031→0.003076元；该次免费8B用量未知，按冻结的零Token单价不增加费用估算，平台实际账单未核。成本与延迟的收益应分开判断。
- N3完整历史/筛选均12/12，无历史6/12。12对完整用量样本中，筛选Token89524→113363（增加26.63%），时延18.536→26.782秒，费用估算0.169857→0.220222元。虽然历史输入较短，筛选组实际工具22→26次、模型28→32次，本批未支持用筛选替换默认完整历史；本轮未修改默认配置。
- 核心新增待修问题是补问任务的不必要计划读取，以及明确星期限制被省略参数绕过。具体错误与原始轨迹见第5节；本轮只测量，不在运行中修正。

## 1. 各设置结果

业务结果检查结束原因、最终数据库状态、必要回答信息及控制动作；完整过程合同还要求预期工具参数/合法画像继承、关键顺序和图依赖。两者都不是开放回答的完整语义质量评分。

| 实验/设置 | 业务结果通过 | 完整过程合同通过 | 全样本时延均值±标准差（秒） | 已知Token合计 | 已知费用估算（元） | 用量未知调用 |
|---|---:|---:|---:|---:|---:|---:|
| N1 主从串行 | 10/12（83.3%） | 10/12（83.3%） | 77.920 ± 23.958 | 173921 | 0.338199 | 4 |
| N1 有界DAG | 10/12（83.3%） | 10/12（83.3%） | 77.156 ± 43.140 | 183460 | 0.357858 | 6 |
| N2 固定强子模型 | 8/12（66.7%） | 8/12（66.7%） | 49.109 ± 36.126 | 293784 | 0.585305 | 0 |
| N2 rules模型路由 | 10/12（83.3%） | 10/12（83.3%） | 47.588 ± 27.213 | 254793 | 0.491134 | 1 |
| N3 最近完整历史 | 12/12（100.0%） | 12/12（100.0%） | 18.536 ± 10.585 | 89524 | 0.169857 | 0 |
| N3 相关历史筛选 | 12/12（100.0%） | 12/12（100.0%） | 26.782 ± 21.226 | 113363 | 0.220222 | 0 |
| N3 不传会话历史 | 6/12（50.0%） | 6/12（50.0%） | 19.116 ± 5.375 | 91147 | 0.176167 | 0 |

时延包含Agent请求、确认/拒绝/重放及越权等控制动作，排除夹具准备、清理和15秒任务间等待。该表包含失败，不能单凭均值把快速失败解释为效率更高。标准差为总体标准差，不是置信区间；有未知调用时Token/费用只覆盖已知部分。

## 2. 同任务成功配对的效率对比

仅在同场景、同重复位置双方完整合同均通过时比较时延；Token/费用进一步要求两边用量均完整。失败数量始终保留在上表，不为效率比较从成功率分母剔除。

| 基线→对照 | 双方成功/全部配对 | 时延基线→对照（均值±标准差秒） | 用量完整配对数 | Token合计基线→对照 | 费用合计基线→对照（元） |
|---|---:|---:|---:|---:|---:|
| N1/serial->dag | 9/12 | 67.169±15.478 → 56.006±25.460 | 8 | 129225 → 129400 | 0.252363 → 0.252514 |
| N2/fixed_strong->routed | 8/12 | 34.774±19.836 → 48.338±25.809 | 7 | 114732 → 129405 | 0.227678 → 0.246867 |
| N3/recent->none | 6/12 | 23.924±12.595 → 20.427±6.302 | 6 | 43031 → 47069 | 0.081528 → 0.091265 |
| N3/recent->focused | 12/12 | 18.536±10.585 → 26.782±21.226 | 12 | 89524 → 113363 | 0.169857 → 0.220222 |

配对数量小，且成功配对是条件子集，不代表全部用户请求的平均加速。若只有少数不同场景成功，结论限于这些场景；相同场景重复不是独立用户样本。

## 3. 逐场景结果

| 场景 | 设置：业务结果；完整合同（各3次） |
|---|---|
| 并列体重/步数查询 | serial：3/3；3/3； dag：3/3；3/3 |
| 身体事实→训练档案依赖查询 | serial：1/3；1/3； dag：1/3；1/3 |
| 体重＋步行联合记录 | serial：3/3；3/3； dag：3/3；3/3 |
| 身高＋运动目标合并 | serial：3/3；3/3； dag：3/3；3/3 |
| 步数事实查询 | fixed_strong：3/3；3/3； routed：3/3；3/3 |
| 实际时长缺失补问 | fixed_strong：0/3；0/3； routed：2/3；2/3 |
| 生成下周计划 | fixed_strong：3/3；3/3； routed：3/3；3/3 |
| 调整既有计划 | fixed_strong：2/3；2/3； routed：2/3；2/3 |
| 从历史引用步行时长 | recent：3/3；3/3； focused：3/3；3/3； none：0/3；0/3 |
| 从历史引用昨天日期/时长 | recent：3/3；3/3； focused：3/3；3/3； none：0/3；0/3 |
| 本轮17覆盖历史23并拒绝保存 | recent：3/3；3/3； focused：3/3；3/3； none：3/3；3/3 |
| 数据库事实优先于聊天数字 | recent：3/3；3/3； focused：3/3；3/3； none：3/3；3/3 |

无历史组缺少完成指代任务的必要信息时，补问可以是合理安全行为，但本批单次续接脚本没有提供补充答案，所以未完成记录目标仍记任务失败。此设置用于测量上下文的作用，不把所有失败都解释成模型幻觉。

## 4. 与修复前的描述性对照

| 设置 | 旧业务结果→新业务结果 | 旧原始过程合同v1→新合同v2 |
|---|---:|---:|
| N1/dag | 3/12 → 10/12 | 3/12 → 10/12 |
| N1/serial | 3/12 → 10/12 | 3/12 → 10/12 |
| N2/fixed_strong | 9/12 → 8/12 | 6/12 → 8/12 |
| N2/routed | 9/12 → 10/12 | 6/12 → 10/12 |
| N3/focused | 10/12 → 12/12 | 10/12 → 12/12 |
| N3/none | 6/12 → 6/12 | 6/12 → 6/12 |
| N3/recent | 12/12 → 12/12 | 12/12 → 12/12 |

业务场景、用户输入和预期终态与旧版一致，v2数据只增加版本/已见集标记/参数合同声明。旧N2过程合同另有事后等价复核9/12，原始6/12不改；本轮v2等价规则在运行前冻结。代码、参数反馈、强模型超时30→45秒、整轮120→180秒、网络重试0→1同时变化，因此上表不将差值归因于单项修复，也不当作同条件A/B。旧结果来源：cc2cea27-5a3d-4ead-8adc-d49394cdee4a/combined。

## 5. 失败、权限与数据副作用

16项失败按主要原因划分如下，分类不改变任何原始分数：

| 主要原因 | 失败数 | 证据与解释 |
|---|---:|---|
| 依赖查询云超时/熔断 | 4 | 两组各2项失败；同一领域模型连续超时后，后续同端点调用可能被熔断，领域未完成。 |
| 缺时长补问时进行不必要的计划读取 | 4 | 固定强模型3项、路由组1项。反复用2026-10-31等非周一日期调用周计划工具，返回INVALID_WEEK_START；部分后续纠正仍聚合为部分失败，另有一次耗尽模型调用预算。当前重复无效参数保护没有阻断这类业务错误循环。 |
| 明确星期限制未进入调整参数 | 2 | 两组第2次均漏传available_weekdays；服务端沿用画像后安排2026-11-10周二12分钟，而目标是2026-11-14周六12分钟。不是仅文字差异，已通过测试脚本确认写入错误测试计划后被终态检查发现。 |
| 去除历史后无法完成指代记录 | 6 | 无历史组日期/时长指代各3项，必要信息缺失后未完成记录目标；不等同于越权或未经确认写入。 |

星期问题是省略覆盖字段导致的继承错误，区别于之前把星期六显式写成6。现有weekday守卫只检查已提供的列表，没有拦住缺省后实际候选违反原始限制的路径。补问错误也说明“发生过业务读取错误”与“后来已正确恢复”需要更精细的状态处理。代码在84次测量中保持冻结，这些问题本轮未修。结构化证据见findings.json、failure-diagnostics.json。

| 检查 | 通过/实际执行 |
|---|---:|
| 确认前业务事实不变 | 84/84 |
| 另一用户事实不变 | 84/84 |
| 外部用户会话访问拒绝 | 84/84 |
| 外部用户确认拒绝 | 45/45 |
| 错误确认版本拒绝 | 45/45 |
| 重复确认/拒绝无额外写入 | 45/45 |
| 同请求重放不新增模型调用 | 84/84 |

这些分母包括只读和提前失败任务，不能据此宣称生产安全率100%。本批并未覆盖全部饮食、视觉、提示注入或所有恢复/并发场景；原软件测试与本批真实模型测量分开。

| 失败位置 | 结束原因 | 未通过规则 |
|---|---|---|
| n1-dependent-query--r1--dag | partial_completed | end_reason |
| n1-dependent-query--r2--dag | partial_completed | end_reason, critical_order, tool_parameter_contract |
| n1-dependent-query--r2--serial | partial_completed | end_reason, critical_order, tool_parameter_contract |
| n1-dependent-query--r3--serial | partial_completed | end_reason, critical_order, tool_parameter_contract |
| n2-adjust-plan--r2--fixed_strong | pending_confirmation | expected_final_state, tool_parameter_contract |
| n2-adjust-plan--r2--routed | pending_confirmation | expected_final_state, tool_parameter_contract |
| n2-clarify-duration--r1--fixed_strong | partial_completed | end_reason |
| n2-clarify-duration--r2--fixed_strong | partial_completed | end_reason |
| n2-clarify-duration--r3--fixed_strong | partial_completed | end_reason |
| n2-clarify-duration--r3--routed | partial_completed | end_reason, asks_duration |
| n3-date-reference--r1--none | completed | end_reason, candidate_presence, expected_final_state, tool_parameter_contract |
| n3-date-reference--r2--none | completed | end_reason, candidate_presence, expected_final_state, tool_parameter_contract |
| n3-date-reference--r3--none | completed | end_reason, candidate_presence, expected_final_state, tool_parameter_contract |
| n3-duration-reference--r1--none | completed | end_reason, candidate_presence, expected_final_state, tool_parameter_contract |
| n3-duration-reference--r2--none | completed | end_reason, candidate_presence, expected_final_state, tool_parameter_contract |
| n3-duration-reference--r3--none | completed | end_reason, candidate_presence, expected_final_state, tool_parameter_contract |

原始失败回答、工具参数、模型错误与run ID见analysis.json的failures/failure_evidence和各episodes目录；不基于一项失败同时触发的多条规则重复计算失败任务数。process-event-counts.json额外记录Schema错误、依赖阻断和实际工具执行；预期工具步骤覆盖率不等于所有模型参数的准确率。

## 6. 冻结设置、费用与证据

- N1：主从串行/master_serial对有界DAG/master，模型和最近历史相同。
- N2：固定强子模型对原rules模型路由，其他条件相同；简单只读路由可用Qwen3-8B，其余仍用强模型。实际选择分布见actual-model-selections.json；两组实际使用同一型号的任务，其结果差异不能单独归因于模型切换。当前日常启动conservative策略不是本轮额外实验臂。
- N3：最近完整/相关筛选/无历史，固定同一强模型与DAG。历史由固定夹具预置，不测真实模型从首轮连续生成六轮对话，也不冒充长期记忆架构对照。
- 主控Qwen/Qwen3-32B，强子模型Pro/deepseek-ai/DeepSeek-V3.2，N2小模型Qwen/Qwen3-8B；温度0、思考关、输出1024；强模型45秒，其余30秒，整轮180秒，网络重试1，步数16/模型12/工具12。
- 本地兜底和自动命名关闭；单任务顺序，DAG子任务并发上限2，场景内方案顺序随重复轮换。Windows/Python3.12.16，进程内ASGI TestClient＋真实stdio MCP＋本机MySQL测试库；不测浏览器首Token或生产吞吐量。
- 本轮已知用量费用估算2.338742元，11次用量未知（10次Pro/DeepSeek、1次公开零单价8B），含25%余量的保护占用7.05782750元，独立上限10元；平台实扣未知。共362次获预算准入的云调用尝试，所有362次都可逐项归属，本轮未发生实验费用预算中断；一次失败任务耗尽的是单任务模型调用次数预算，二者不同。未知Token不按0补齐。
- 168个本批合成用户独立查询剩余0；旧849、919及修复1008项封存逐一复核均一致。旧报告、旧账本、原始失败及补跑记录不覆盖，该实验批次未发布代码。
- 当前仍是12个已见场景，每个实验只有4种业务场景，3次重复不是独立用户。原dev/holdout标签只保留历史分组；没有新的盲测或独立人工/模型语义评分。

数据目录：`artifacts/experiments-v1/15832c96-6ad3-4c2b-9a29-163354decc21`。

逐项结果episodes.csv；聚合analysis.json/table.md；配对比较analysis.json/paired；初/中/末数据库状态和原始模型回复在episodes；费用budget-summary.json与budget-ledger-snapshot.json；清理cleanup-audit.json；旧数据完整性historical-integrity.json；源码/数据/设置protocol.json、dataset.jsonl、source.zip；环境runtime-environment.json；最终证据索引manifest.json。

只读复核请读取上述JSON/CSV。分析命令会写本批统计文件；封存后要先复制到副本，再运行随批保存的analyze_experiments_v2.py。

本轮结束后如需进一步优化，应以失败轨迹提出单项改动，再用另建未见集验证；本报告不自动触发新的开发或付费实验。
