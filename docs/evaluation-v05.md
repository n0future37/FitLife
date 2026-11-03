# 0.5评测与复现

工程测试检查工具、事务、权限、确认、恢复和故障；Agent评测额外检查路由、工具参数/顺序、最终事实与副作用。
固定响应验证执行机制，真实模型验证当前服务条件下的实际表现，二者不混为任务成功率。

## 命令

在项目根使用fit完整解释器，先设`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`和既有Redis凭据文件路径。

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m pytest -p pytest_asyncio.plugin --integration -q
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate --split dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate --split holdout
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate_multi --split dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate_multi --split holdout
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.ui_check
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.media_ui_check
```

原core-v1.2仍显式用single模式回归，分组开发11/保留4；不能当作新多Agent质量指标。
新multiagent-v1按任务组拆分开发3/保留2，包含合并写入、前序查询、统一记忆、拒绝和恶意模型调用。
每项包含初态、输入、脚本、允许工具、禁止副作用、最终事实、控制操作与清理规则。
恶意模型调用用例故意输出越权工具，只验证服务端边界，不作为真实模型应该模仿的答案。

规则检查确认前事实不变、另一用户不变、最终画像/记录/活动数值、允许工具和关键依赖顺序，
并执行跨用户确认、重复确认/拒绝和请求重放。报告记录数据集和代码摘要、运行ID、实际耗时与模型尝试。
开放语义整题成功率保持null，不能把规则覆盖不到的解释正确性当成已通过。

## 显式付费入口

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.paid_check --live --budget-usd 1.5 --repeat 2
```

该入口共用`artifacts/paid-checks/budget-20261102.json`，不重置已有预留。
顺序检查一项跨域任务、已知合成标签、4秒无动作合成视频和Chat工具往返；素材没有旧个人数据。
本次最终四组各重复两次，仍是小样本验收，不代表真实素材准确率、性能分布或稳定改进。
标签真值来自程序生成的文字；核对由脚本模拟，不计作独立人工标注。
普通回归不运行这些付费入口，生产媒体分析本身会产生费用。

`api_script_seconds`含用户控制、请求重放和API/MCP/模型等待，排除fixture建立与清理，不是纯模型推理延迟。
媒体轨迹不保存图片、用户文字或识别结果，私有结果在本人数据库。已返回usage按实际记录，缺失保持未知；
provider_request_id可用于核对平台日志。预留金额/最高组价估算与实际账单分开。

## 比较与失败归因

先冻结数据与规则，再比较固定领域模型/保守选择，或DAG并发/串行等单一改动。
共同业务范围内比较才有可解释性；不拿运动单Agent不支持饮食的结果宣称多Agent推理提升。
分别归因任务清单遗漏、规划依赖、工具参数、服务规则、供应商超时和综合回答。
真实模型有随机性，后续更大预算评测需分组重复并报告样本数、波动和执行条件。
本次旧报告、失败报告和0.4归档保留，不覆盖后改称原来通过。

2026-11-03实测：首次四组中跨域和标签未通过；跨域暴露写入工具被错误硬绑定，
后修复只读依赖契约；标签生成器在Windows使用CRLF导致文字越界被裁，模型正确保留不可见项为null。
修正素材生成器为LF后重新验收，不将此素材修正当作模型识别能力提升。
最终报告`artifacts/paid-checks/85fa3126-4766-4a86-adae-dfa44e8c7788/report.json`为8/8。
首次报告`2e8fde06-3f45-4af3-8d82-6bc9e77c8883/report.json`及各次源码包保留。
累计预算记账0.449780703125美元是最高公开组价加余量的估算，实际扣费尚未核账。

同一份冻结开发集的固定响应对照：默认DAG 3/3、串行DAG 3/3、独立领域策略2/3。
串行报告`artifacts/multi-evaluations/b8119826-9903-4299-a7fc-c6b79c59718c/report.json`，
独立报告`c3fd756d-27f2-4bf2-9d7e-0e9d2b9b6b79/report.json`。
独立策略在依赖查询中先执行训练档案查询，违反明确的前序顺序；其他规则通过，没有额外写入。
这验证独立策略不保证跨域前序依赖，不是随机真实模型质量或速度提升的证据。
