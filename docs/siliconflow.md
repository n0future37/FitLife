# 硅基流动接入与本机启动

2026-11-03。已直接使用Windows用户变量SILI_API_KEY，认证及模型目录通过；密钥未输出或写入项目。用户报告账户有20元，旧/user/info接口实测410，未自动核实余额与实扣。用户后续明确允许按开发需要用模型、费用明显增加再降档，不将余额自动转成必须花完的预算。

## 当前模型配置（修复v2.2）

config/siliconflow.models.json中的云端文字/视觉配置共用SILI_API_KEY，默认推荐脚本使用conservative策略：

| 配置 | 模型 | 当前用途与限制 |
|---|---|---|
| fast | Qwen/Qwen3-8B | 无历史、单领域、明确只读查询；关闭思考、温度0、单次30秒 |
| control | Qwen/Qwen3-32B | 主Agent意图清单/规划/综合；关闭思考、温度0、单次30秒 |
| standard | Pro/deepseek-ai/DeepSeek-V3.2 | 历史、多领域、计划及其他交付；关闭思考、温度0、单次45秒 |
| complex | Pro/deepseek-ai/DeepSeek-V3.2 | 当前与standard同配置；保留别名，不声称默认开启或动态切换思考强度 |
| vision | Qwen/Qwen3-VL-8B-Instruct | 沿用原图片配置；本轮未扩大视觉质量验收 |
| local | 本机Qwen3 4B Instruct | 已运行Ollama服务下的文本只读备用；不代替写入、图片或超大上下文任务 |

启动脚本启用至多1次瞬时故障重试、180秒主流程上限及既有熔断/只读备用。可显式加`-RoutingPolicy rules`切回规则策略；默认调整依据修复验收，不代表已证明统计优势。最初接入时standard/complex均为32B、complex开启思考的配置属于历史，当前已替换。

本轮112软件测试、最终固定28流程、最终v2.2两项真实日常API和三次本地故障切换通过。已知用量费用估算0.564587元，另有5次超时用量未知，不冒充实扣；失败、条件差异、账本和来源见[Agent问题修复](agent-repairs-v2.md)。

SF Chat使用max_tokens及显式enable_thinking/thinking_budget，不把OpenRouter的reasoning_effort原样冒充为同义强度。另对免费Qwen3-8B实际开启思考测试，算式回答42、reasoning_tokens=129；工具测试关闭思考时为0。任务不同，不作同题A/B效果比较；请求thinking_budget=128，实际129，不能将其当作严格Token硬上限。硬时间、步骤、工具白名单、共享预算仍由服务端约束。

## 首次接入时点的验收与费用（历史）

- 双文字模型各一次get_activity_catalog真实往返，返回ID与请求一致；原工具参数与结果回传保持一致。
- 主Agent路由→运动子Agent→真实MCP/MySQL查询预置8426步，回答正确；同请求Key重放同run、业务事实不变、无待确认写入、合成用户清理。run ff259150-396c-42a0-b7d7-c8708d2c8e95，共3个真实云模型调用，degraded=false。不是三领域联合/写入/完整多轮质量验收。
- 首个免费8B工具结果后45秒超时；将短回答输出从768收紧为256后复核通过。自绘位图卡字形不清导致首个OCR检查失败，换成本机Arial测试卡后通过；未放宽WALK/20断言。失败a29cb710-5606-4c48-b757-4c540aca334d、d4f904b4-dca6-4285-9d0a-b6002186a5b1保留；后者双模型与思考证据经核心源码/配置SHA一致核验，复用到最终报告，没有重复花费冒充独立重测。
- 全程12次尝试，11次得到Token用量、1次免费模型超时未知用量；累计按官方单价估算0.0010010元。实际扣费/最新余额未知，不能称为实扣。价格快照：8B免费；32B输入1/输出4元每百万Token；VL8B输入0.5/输出2元。来源https://www.siliconflow.cn/models，原始快照在每批prices.json。
- 本轮账本预算0.5元/12次，未知用量保留预留，图片按完整上下文保守预留，不从原ModelFlare美元账本扣减。该批次数已用完；后续新验收按新任务安排预算，不删除旧账本、也不自动重跑。普通server启动不套用此一次性验收账本，会按实际调用产生费用。
- Windows83/83、Ruff通过（regression.xml），新增3项参数/熔断配置隔离/预算重启与未知预留测试；本轮无新云CI/Linux，不把旧云结果用于新增改动。页面黑白风格已完成32项浏览器验收，见[黑白页面](design-monochrome.md)。

最终报告artifacts/siliconflow-checks/7de79688-841d-4953-95cc-d414cccf6df9/report.json；复用来源d4f904b4-dca6-4285-9d0a-b6002186a5b1/report.json；人民币账本budget-cny-v1.json；摘要/源码SHA为manifest.json。报告秒数只是单次连接/机制样本，不代表生产延迟或通用任务成功率。

## 启动

本机推荐入口自动读取现有SILI_API_KEY用户变量，不需要在旧PyCharm终端重新粘贴密钥；环境选择dev，脚本结束恢复进程原配置。数据库凭据沿用Windows凭据管理器。

```powershell
Set-Location H:\Tomorrow\FitLife
& .\scripts\start_siliconflow.ps1
```

打开http://127.0.0.1:8100/，填写个人访问令牌。首次令牌和数据库迁移仍按README中的manage说明，不把SILI密钥填进网页。脚本语法与相同配置的API已验证，未在本轮额外启动常驻Web服务。

需要本地备用时先在另一个终端运行：

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.local_model serve
```

既有OpenRouter免费配置和ModelFlare入口保留。项目通用Settings默认值未全局改写；通过本机推荐脚本显式选择硅基流动，不修改其他项目或Windows全局FITLIFE配置。

验收入口为`python -m fitlife.siliconflow_check --live`，它需要SILI_API_KEY在当前进程中；本批累计次数已用尽，直接重跑会被守卫阻断，先按下一轮任务制定批次再调用。普通回归不需要模型密钥、不产生API费用。
