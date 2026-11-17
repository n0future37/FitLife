# 模型选择、平台与协议

业务路由决定交给身体、饮食还是运动Agent；模型选择决定这一步使用哪份模型配置。
同一个Agent可以查询时使用fast，复杂任务使用standard，图片识别使用vision。

默认standard为ModelFlare/gpt-5.6-sol，fast为gpt-6-luna，vision为现有已验证图片输入的模型。
保守策略仅对无历史、单领域、明确只读且必需工具均为只读的任务选fast，其他任务选standard。
路由默认fast，多领域规划与综合默认standard。结构化规划错误最多修正一次，仍共用总预算。
暂时网络错误有限重试，不因业务规则失败自动升级模型，不绕过确认或写入权限。

同一密钥能否访问多个模型由平台授权决定；普通key可受路由组限制，Smart Key也只覆盖账户已授权范围。
本次现有key已通过两个不同模型ID的实际工具/图片探测。各模型也可使用不同key_env/key_file，
不要求一个密钥覆盖所有供应商。参见[ModelFlare Smart Key](https://docs.modelflare.dev/guides/features/smart-api-key/)。

## 配置

默认配置不必创建文件。需要第二平台或本地服务时，复制`config/models.example.json`到自选的无秘密配置文件，
通过`FITLIFE_MODEL_PROFILES_FILE`指定路径。每项声明name/provider/base_url/api_family/model/key_env/key_file/capabilities。
只允许凭据来源，不允许把api_key写进配置。URL中不允许用户名、密码和查询参数；外部服务使用HTTPS，
本地HTTP仅允许127.0.0.1/localhost/host.docker.internal。

- `FITLIFE_ROUTER_MODEL`、`FITLIFE_PLANNER_MODEL`、`FITLIFE_SYNTHESIS_MODEL`、`FITLIFE_EASY_MODEL`、`FITLIFE_VISION_MODEL`引用配置名。
- `FITLIFE_ROUTING_POLICY=fixed`固定领域模型；`conservative`启用上述选择规则；`rules`按配置文件顺序匹配领域/任务类型/历史/依赖，并引用模型及推理强度配置。
- `FITLIFE_ORCHESTRATION=master`为默认主从；`master_serial`为串行对照，`master_independent`为独立领域策略，`single`为原运动基线。
- 模型请求带全局超时、最大调用数、输入字节和保守token单位预算；各子Agent不能各自重置总预算。

支持显式Responses、Chat Completions与Ollama原生适配。Chat适配转换工具调用ID、工具结果和图片内容块；
不自动猜协议。不同平台可能有额外字段差异，配置能力不等于已真实验证兼容。
本机Ollama与精确标签qwen3:4b-instruct-2507-q4_K_M已部署；真实MCP/MySQL只读查询及超时降级、重放验收通过。OpenRouter密钥已配置，同一Key的NVIDIA/Cohere免费模型真实切换和只读工具往返通过，费用为0；付费示例及视觉未验收，见[免费接入](openrouter-free.md)。默认云端配置仍为ModelFlare；备用仅覆盖文本与只读工具。细节与启动命令见[模型可靠执行](model-resilience.md)。

图片理解通过文字模型的图像输入，不使用图片生成或视频生成接口。
视频通过本地FFmpeg采样后送视觉模型。参见[OpenAI图片输入说明](https://developers.openai.com/api/docs/guides/images-vision)。

## 费用

普通回归固定模型响应，不产生API费用。真实验收必须显式启用。
本轮用户授权新增验收预算1.5美元，`evaluation_budget.py`按2026-11-02公开最高适用组价、额外输入预留和25%余量记账，
每次尝试先写预留；已知用量按最高公开组价加25%余量结算，未知用量保留完整预留。
policy-v2迁移保留原9次调用与v1账本备份，不重置1.5美元上限。预算守卫只用于本轮受控验收，不能替代平台金额配额。

实际扣费以平台消费记录为准，报告actual_billed_usd在未核账时保持null。
0.6在同一上限内增加带来源的累计账单检查点：用户报告此前共用0.25美元，原33次调用及其估算完整保留。
检查点以后继续累计未知调用预留和已知用量估算；不会因余额1.8美元自动增加预算。
对照结果与供应商模型标识差异见[核心验收](core-v06.md)，每任务实际费用仍未知。
价格来源：[gpt-5.6-sol](https://modelflare.dev/pricing/gpt-5.6-sol)、[gpt-6-luna](https://modelflare.dev/pricing/gpt-6-luna)。
以后重新运行应先核对最新价格，不能把此次快照视作长期固定报价。

容器自定义注册表时为每个外部模型明确指定对应secret文件key_file，并只读挂载无秘密的配置文件；不假设宿主机环境变量自动进入容器。

硅基流动已新增实际验收：`config/siliconflow.models.json`，四份云配置共用SILI_API_KEY，显式支持enable_thinking/thinking_budget与max_tokens，详见[接入与本机启动](siliconflow.md)。通用默认值保留，本机推荐脚本选择该平台；复杂配置与每种媒体任务分别看其验收边界。
