# OpenRouter 免费模型接入与切换

2026-11-03本机验收通过。用户明确只用免费模型、不充值；已将Windows用户变量OPENROUTER安全同步为OPENROUTER_API_KEY，原变量保留。密钥不写项目、报告或日志。

同一密钥的两个配置：

| 路由条件 | 配置名 | 本次实际请求及返回模型 |
|---|---|---|
| 无历史的运动查询 | fast | nvidia/nemotron-3.5-lightning:free |
| 带历史标志的运动查询 | standard | cohere/north-mini-code:free |

配置位于config/openrouter.free.models.json与config/openrouter.free.routing.json。免费模型目录会变化；CLI每次重新核对公开价格全部为0、支持tools，目录不满足即停止。这里只读合成场景，不改变应用默认ModelFlare配置，也未启用付费/视觉示例。

## 这次验证了什么

项目select_model规则选择 → resolve/configured传递模型 → 现有Chat适配实际请求 → 模型提出get_activity_catalog(category=all) → 执行项目真实只读函数 → 原调用ID回传结果 → 模型回答步行、轻柔关节活动。两个模型均通过，最终4次HTTP模型请求全部200，返回模型ID不同且与请求一致。

路由中的历史为受控的有/无标志，不是完整多轮记忆质量评测。没有启动完整主Agent分解/综合、MCP或数据库写入；此次不能作为端到端业务成功率、视觉能力或通用任务质量指标。各模型只测1个工具往返。

free_only在配置层拒绝付费模型和非OpenRouter端点，Chat请求再校验；请求限定:free并发送provider.max_price的prompt/completion/request/image全0及require_parameters=true。没有models付费备选、插件、SDK自动重试，验收关闭本地兜底，避免本地成功冒充云接入。CLI默认最多6次模型尝试、每次45秒、总150秒；本次通过实际4次。

实际发现两个Gemma免费端点429，原失败报告保留。换成其他免费模型后成功；不以充值解决、不无限重试。平台费用字段均0，账户usage前后0，delta=0；免费请求计数可能延迟，不用其差额代替本地实际调用数。首次失败1次、第二批3次（NVIDIA2成功/Gemma1限流）、最终4次成功，全程8次模型HTTP尝试，其中6次成功/2次429；不将尝试过程包装成100%模型可用率。

通过报告：artifacts/openrouter-free/70dab4bf-79df-4709-b3f6-2e69d0d7f7cf/report.json。失败eec0fe42-ed9d-4ee3-8521-dbaf4b54c992与3129ad10-931c-4c25-b92b-217e7e6f9f54保留。配置快照在最终报告同目录；摘要/哈希见artifacts/openrouter-free/manifest.json。

软件回归80/80及Ruff通过，新增2项验证免费模式阻断付费/其他端点、传递0价限制及运行时再次校验；真实云验收不混入普通pytest。不重跑前一轮32项浏览器检查，新改动未上传Git、未做云CI/Linux。

## 再次验收

仅需要重新核验时运行，会消耗免费请求额度，不会充值；已打开的PyCharm可能尚未继承新用户变量，所以在同一终端从用户配置加载一次，不打印密钥：

```powershell
Set-Location H:\Tomorrow\FitLife
$env:PYTHONIOENCODING = 'utf-8'
$env:OPENROUTER_API_KEY = [Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY', 'User')
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.openrouter_check --live-free
```

这里只验证免费文字与工具协议。付费模型、视觉、推理强度变化和完整多Agent场景仍需单独设计并显式授权相应调用，不因免费验证通过而自动启用。

官方依据：[免费变体](https://openrouter.ai/docs/guides/routing/model-variants/free)、[供应商价格上限](https://openrouter.ai/docs/guides/routing/provider-selection#max-price)、[额度接口](https://openrouter.ai/docs/api_reference/limits)。
