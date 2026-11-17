# 模型路由、熔断与本地兜底

2026-11-03。先完成OpenRouter配置与本地兜底，后按用户授权完成两个免费云模型验收，未充值/付费。最新免费验证见[OpenRouter免费接入](openrouter-free.md)；下文付费示例仍待单独验收。本机部署状态见STATE。

## 一个密钥切换模型

[OpenRouter](https://openrouter.ai/support/)提供统一API及密钥，通过请求的model字段选择平台目录中的不同模型；[模型与供应商回退](https://openrouter.ai/docs/guides/routing/model-fallbacks)是其额外的平台功能。本项目仍自行记录目标模型、供应商、推理配置、耗时与回退原因，便于复核。账户权限、余额、地域及模型能力仍需真实账户验收，“一个Key”不等于任意模型和参数都可用。

`config/openrouter.models.example.json`是无秘密示例。standard、fast、complex、vision共用OPENROUTER_API_KEY；local使用本机Ollama原生API，不需要云密钥。示例中的模型ID和tools/images/reasoning字段已只读核对公开目录，付费示例尚未真实调用；另有专用免费配置已完成真实验收。

复杂与标准配置可以指向同一个模型，分别设置high/low推理强度；也可以选择不同模型。推理强度按所选配置传递，参数支持以平台/模型为准。

`FITLIFE_ROUTING_POLICY=rules`启用规则文件，按顺序选择第一个匹配项；支持领域、任务类型、是否允许历史和依赖。无匹配项用standard。默认conservative行为保留，fixed仍可用。领域路由模型只提取用户任务，不直接获得修改平台地址或任意模型名称的权限。一次子Agent执行期间保持选定配置；暂不实现执行途中按推理质量自动升级模型。

## 超时、重试与熔断

- 每次模型尝试由asyncio.timeout提供硬超时；模型配置可指定request_timeout，外层整个Agent请求仍有总时间限制。媒体每次调用不超过45秒，配置更短时保留更短值。
- 408/429/5xx、连接失败、网络超时计入暂时故障；401/403、错误参数和业务校验失败不自动重试或切换。调用方取消直接传播，不启动备用调用。
- 每个进程按供应商/地址/模型/凭据来源/推理配置维护熔断状态。默认连续2次暂时失败后打开30秒；打开期间跳过网络调用，冷却后只放行一个半开探测。旧请求的迟到成功不能关闭新熔断。
- 这是进程内熔断，不声明Redis共享的跨实例熔断；原Redis跨实例容量控制继续生效。
- 开关与参数：FITLIFE_CIRCUIT_ENABLED、FITLIFE_CIRCUIT_FAILURE_THRESHOLD、FITLIFE_CIRCUIT_COOLDOWN。FITLIFE_MODEL_RETRIES控制同模型有限重试，使用快速备用时建议设为0。

## 本地兜底的能力边界

默认fallback_profile为local，默认注册表已包含该配置；自定义注册表也应显式包含local。设置FITLIFE_FALLBACK_PROFILE为空字符串可关闭备用。真实网络模型的暂时失败或熔断可转到本地，固定响应测试不会暗中调用本地模型。

首版备用支持文本和只读工具。遇到带写工具的目录或图片输入时有界失败，不让文本小模型冒充视觉分析，也不绕过现有权限、确认、版本与事务规则。界面的直接业务表单与只读API仍可使用。复杂任务或上下文过大时不能保证降级完成，须明确未完成状态。

同一执行账本中，云端失败及本地尝试分别计数，不重置调用/输入预算；未知云端用量保留预留，不记为零。本地输入限制12000 UTF-8字节，上下文16384，关闭thinking；输出仍受调用预算限制。同一请求对已降级的模型保持本地，避免中途反复切换。

Responses工具调用历史可转为Chat消息，保留call_id和既有工具结果，不重新执行已发生的工具。模型返回仍经过原有工具白名单、参数及依赖检查。成功降级的API返回degraded=true，回答明确提示使用了本地备用；运行轨迹记录实际本地模型标识。

## 本机部署与启动

- 运行时：官方Ollama v0.40.13 Windows便携版，E:/Work/Apps/Ollama/v0.40.13。发布包SHA256：7afa38624fa62ee5d8293422d0ab084c1bb4603134c7f4dafce11a033b57cc19。
- 模型：Qwen3 4B Instruct 2507 Q4_K_M（精确标签qwen3:4b-instruct-2507-q4_K_M），权重及元数据位于H:/Tomorrow/FitLife/data/local-models，正式模型文件约2.50GB。只监听127.0.0.1:11435；单并发、最多加载1个模型、队列最多2个。
- Ollama原生下载遇到本机代理fake-IP解析与重定向防护冲突；本次从官方注册表清单做TLS校验的分段下载，并核对每个文件SHA256后写入本地模型目录，没有关闭TLS或修改代理。Instruct来源和哈希记录artifacts/local-model-20261103/instruct-download.json；锁定清单0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0，可运行`python scripts/pull_local_model.py --fetch`恢复。
- 本地服务未注册开机自启。重启电脑后，在项目终端运行serve，保持该终端运行；Ctrl+C关闭本次启动的服务。另一终端运行FitLife。

```powershell
Set-Location H:\Tomorrow\FitLife
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.local_model serve
```

另一终端可检查与预热：

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.local_model status
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.local_model warmup
```

开启OpenRouter时，在启动FitLife的同一个终端设置：

```powershell
$env:FITLIFE_MODEL_PROFILES_FILE = 'H:\Tomorrow\FitLife\config\openrouter.models.example.json'
$env:FITLIFE_ROUTING_POLICY = 'rules'
$env:FITLIFE_ROUTING_RULES_FILE = 'H:\Tomorrow\FitLife\config\routing.example.json'
$env:FITLIFE_ROUTER_MODEL = 'fast'
$env:FITLIFE_PLANNER_MODEL = 'complex'
$env:FITLIFE_SYNTHESIS_MODEL = 'standard'
$env:FITLIFE_FALLBACK_PROFILE = 'local'
$env:FITLIFE_MODEL_RETRIES = '0'
```

等用户准备好账号与预算后，在本机隐藏输入密钥；不要发送到聊天或写进JSON/项目文档：

```powershell
$openRouterSecret = Read-Host 'OpenRouter API Key' -AsSecureString
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', $openRouterSecret).Password
Remove-Variable openRouterSecret
```

之后沿用README的server启动命令。没有密钥时初始化会明确失败，不将缺少配置误报为云服务超时。原付费评测守卫仅核算已定价的ModelFlare模型；新平台真实评测须先补价目/预算配置，不能复用旧估算冒充新账单。

## 本次实际发现的失败

最初使用的qwen3:4b别名实际指向Thinking 2507；本机/api/show显示thinking.values仅为true。设置think=false及修改提示前缀均未得到适用的非思考行为，不能把参数已传递当作模型已遵守。首次工具协议往返通过，但真实Agent结构化路由失败，原报告artifacts/model-resilience/fcac5678-b362-4a1c-877e-d499f41a35c8/report.json保留。

最终改用官方精确Instruct标签，不使用实验派生的fitlife-qwen3-4b模型。原Thinking权重和失败记录保留作诊断缓存，不计入已验收备用能力。首次warmup.json仅说明载入成功，其截断输出不是任务成功证据。最终功能验收使用Instruct及已知合成步数7312，检查查询回答、工具实际执行、事实不变与请求重放。

## 验收

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m pytest -p pytest_asyncio.plugin --integration -q
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.resilience_check --local-live
```

第二条使用真实本地模型、注入云端故障、合成测试用户和真实MCP/MySQL，只清理本次用户，不调用付费云服务。检查本地工具往返、真实查询、重复请求恢复，并保留原始运行轨迹；它是故障切换机制验收，不能当作本地模型整体任务质量、视觉能力、生产容量或零故障保证。

## 页面启动与验收证据

页面回归发现脚本尚未绑定登录事件时，过早提交原生表单会以默认GET跳转。现已改为默认POST，登录按钮初始禁用，全部应用脚本完成后才启用；浏览器检查也等待明确的appReady状态。新增`python -m fitlife.startup_ui_check`主动延迟脚本，检查提前点击被阻断、令牌不进入URL、加载完成后能正常登录。仅使用已清理的合成测试令牌；原失败报告及脱敏诊断保留，不删除失败重算结果。

最终各项报告、源码SHA256及验证边界集中于`artifacts/model-resilience/manifest.json`。当前新增未提交Git或运行云CI；旧0.6的云CI/Linux结果不能覆盖本轮增量。真实本地验收注入20毫秒云超时，证明切换机制可执行；它不是实际云故障比例或整个系统可用率测量。
