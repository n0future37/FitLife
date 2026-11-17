# FitLife 实验v1：预算中断补跑

日期：2026-11-03。用户明确要求重跑预算中断。本页为最新补测结果；原报告及原84条记录保持历史只读。

## 补跑范围与可比条件

原5条中断涉及N3两个用例的第三次重复。按事前确定的补跑方案，重跑完整三方案组共6条（包含1条原成功对照），无论成败均采用这6条新结果填入对应位置；保留原始90次实际运行，派生84个完整可比位置。没有重跑或删除其他模型/供应商/逻辑失败。

复用原冻结输入、初态、日期、模型、提示词、工具、判分与方案顺序；核对全部源码，唯一执行器语义变化是从协议读取预算上限，默认仍5元。旧5元账本及未知用量预留不变，补跑独立上限2元，累计保护上限7元。官方当前价格与原快照核对相同。

补跑不同于原运行时段，不是新的未见测试集；补齐分母不能当作模型优化提升。N1/N2沿用原24+24条，N3保留原开发18条和原保留12条、补入6条，恢复各方案12次、各场景3次有效测量。

## 六条补跑结果

| 用例/方案 | 通过 | 耗时秒 | 模型尝试 |
|---|---|---|---|
| n3-explicit-override/focused | 是 | 35.970 | 2 |
| n3-explicit-override/none | 是 | 51.309 | 3 |
| n3-explicit-override/recent | 是 | 35.252 | 2 |
| n3-history-not-fact/focused | 是 | 25.551 | 3 |
| n3-history-not-fact/none | 是 | 18.165 | 3 |
| n3-history-not-fact/recent | 是 | 14.690 | 3 |

6/6完整合同与业务结果均通过，0条再次预算中断。

## 补齐后的84位置结果

| 实验/方案 | 业务结果规则 | 原始完整合同v1 | 等价合同v1.1（事后） |
|---|---|---|---|
| N1/dag | 3/12 | 3/12 | 3/12 |
| N1/serial | 3/12 | 3/12 | 3/12 |
| N2/fixed_strong | 9/12 | 6/12 | 9/12 |
| N2/routed | 9/12 | 6/12 | 9/12 |
| N3/focused | 10/12 | 10/12 | 10/12 |
| N3/none | 6/12 | 6/12 | 6/12 |
| N3/recent | 12/12 | 12/12 | 12/12 |

完整合同仍保留原判分；v1.1仅单列此前已验证的合法画像复用，生成计划6条的评分修订不代表模型提升。当前路由/规划与训练明细读取义务的失败仍全部计入，尚未在本轮修复。


| experiment/arm | rule pass | outcome pass | latency mean±sd (s) | known CNY | unknown calls |
|---|---|---|---|---|---|
| N1/dag | 3/12 | 3/12 | 17.908 ± 10.589 | 0.070391 | 0 |
| N1/serial | 3/12 | 3/12 | 34.627 ± 35.908 | 0.101310 | 2 |
| N2/fixed_strong | 6/12 | 9/12 | 29.733 ± 19.215 | 0.291636 | 1 |
| N2/routed | 6/12 | 9/12 | 23.435 ± 18.391 | 0.246992 | 0 |
| N3/focused | 10/12 | 10/12 | 18.282 ± 8.803 | 0.155525 | 0 |
| N3/none | 6/12 | 6/12 | 25.969 ± 10.096 | 0.181513 | 0 |
| N3/recent | 12/12 | 12/12 | 15.421 ± 7.112 | 0.159994 | 0 |


表中混合了成功与失败样本的耗时；只作描述，不能据此宣称整体加速。配对统计、每例重复波动、工具参数、状态和轨迹见analysis.json及episodes目录。合成12场景不足以估计真实用户总体成功率；N3复用部分干扰历史，不作独立会话分布泛化结论。

## 费用、清理与证据

补跑新增已知费用按公开价和Token估算 **0.079673元**，未知用量0次；含全部先前预检与本轮累计已知估算1.55614502元，先前未知用量仍保留，实际账单未独立核验。

原168名与新增12名合成用户合计180名，最终数据库只读核验剩余0；不清空其他用户或开发库。5项实验回归及Ruff通过，Agent业务代码未更改。

- 原始批次：`artifacts/experiments-v1/51ef75e6-f31d-4bdc-afff-66b7dd0ce369`。

- 补跑批次：`artifacts/experiments-v1/cc2cea27-5a3d-4ead-8adc-d49394cdee4a`，含独立protocol/source/budget/summary及6条轨迹。

- 最新合并：`artifacts/experiments-v1/cc2cea27-5a3d-4ead-8adc-d49394cdee4a/combined`，含84位置CSV/analysis/原始合同和事后合同。protocol的episode_provenance/source_sets及merge-provenance.json记录每条来源，两个source ZIP分别对应版本。

- 离线复核：`python scripts/analyze_experiments.py artifacts/experiments-v1/cc2cea27-5a3d-4ead-8adc-d49394cdee4a/combined`；`python scripts/rescore_experiments.py artifacts/experiments-v1/cc2cea27-5a3d-4ead-8adc-d49394cdee4a/combined`。不调用模型；双源码版本以protocol/source ZIP为准。`combined`为分析产物，不能作为新的真实执行批次。
