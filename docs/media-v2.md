# FitLife 多模态补齐与真实评测（media-v2）

2026-11-16。功能闭环已接通：上传完整力量训练视频→本地姿态/周期分析→核对或更正次数→生成候选→用户确认→MySQL保存；餐食图片→视觉粗估→核对/确认→照片估算餐食记录。**真实评测显示精度不足，目前是实验性计数与餐食粗估，不能描述为准确自动计数或精准热量核算。**

## 完整视频怎样处理

- 首先由用户选择深蹲、俯卧撑、弯举、仰卧起坐、引体向上或卧推；只支持单人、单种动作、MP4/WebM、≤80MB/180秒，不做自动动作分类或跨视频/多动作分组。
- OpenCV/FFmpeg解码到EOF，沿整个时间轴以最高15fps运行MediaPipe Pose Landmarker Full v1。完整时间轴不等于每个源帧都做姿态推理，也不等于总数正确。
- 始终选择同一侧关节，2D角度经短窗口平滑，用完整往返周期计数；开发集上由固定角度门槛调整为按10%/90%角度分位数估计幅度、使用20%/80%门槛，最小幅度35度。周期时间戳、角度特征、有效门槛与权重SHA均保留。
- 多人、关键关节可见率不足70%、缺失超过2秒或时间轴不完整时不给整段总数；遮挡间隔不拼成完整周期。其他动作可选仅稀疏观察，不能用静帧声称完整计数。
- 本地独立子进程不传云模型密钥；235秒外层超时、210秒计数运行上限，取消/租约失效会结束子进程，旧nonce不能发布迟到结果。自动证据不可通过核对接口改写；用户修正次数保存到明确确认的业务记录，保留原估计。
- UI显示“自动估计”、周期与可见率，支持更正、候选确认和刷新恢复。推断动作安全、疼痛原因、治疗建议不在能力范围。

## 餐食怎样估算

使用硅基流动Qwen/Qwen3-VL-8B-Instruct，现有Chat适配层与后台媒体任务。图片缩放至最长边640、JPEG后输入；只传图片与通用请求，不传文件名、参考热量、称重或作者计数。逐食物返回可空重量和该份总热量；新提示词明确允许按常见份量粗估、约5g/5kcal取整，并说明用油/酱汁等误差。无法辨认仍留null，不强制补0。未知部分的已知小计不算整餐预测。

最终值仍须用户核对并确认，保留`vision_estimate_user_confirmed`来源，和食物库精确按克数核算分开。模型回答、格式修复及网络失败均进入账本；无模型自批确认权限。

## 数据与冻结方法

- 餐食：[官方Nutrition5k](https://github.com/google-research-datasets/Nutrition5k)，4张开发、8张保留。使用官方depth train/test划分，固定SHA种子选取可用的俯视RGB图，保留排除原因。参考值来自称重食材与USDA营养数据推算，不是实验室直接测热量。官方CC BY 4.0归属保留，不代表中国日常餐食分布。
- 视频：[官方RepCount](https://svip-lab.github.io/dataset/RepCount_dataset.html)与[作者TransRAC标注](https://github.com/SvipRepetitionCounting/TransRAC)，从[公开Lance镜像](https://huggingface.co/datasets/lmms-lab-eval/repcounta-lance)按样本读取15段而非整个大文件。每个作者count与镜像逐样本交叉核对。5类动作各1开发+2保留；弯举没有真实计数结果，只有软件规则测试。
- 视频按已有stu前缀分组排除已选来源，并预设8MiB单样本下载限制。trainNNN等片段的原视频身份不完整，无法保证所有同源镜头都被识别；是小规模公开样本实验，不是严格外部盲测或全量官方榜单。镜像许可字段为other，素材仅本机研究评测，不随源码镜像分发。
- 选样均在预测前完成；v1开发集固定门槛、v2开发集修正及餐食提示词调整分别保留。`frozen-evaluation.json`与`source-evaluation-frozen.zip`在保留集运行前保存。保留集结果出现后没有再调计数门槛、提示词或计分规则。
- 每次运行保存数据/模型/提示词/代码版本、素材SHA、原响应/轨迹、错误、耗时和费用。模型temperature/seed采用供应商默认，未声称完全确定；视频CPU重复测量观察一致性。重复运行不是更多独立素材。

## 保留测试集结果

| 项目 | 餐食照片 | 力量训练视频 |
|---|---:|---:|
| 独立素材与运行数 | 8张×3＝24次 | 10段×2＝20次 |
| 给出完整估计 | 23/24（95.8%） | 10/20（50.0%） |
| 弃权/未给完整值 | 1/24 | 10/20 |
| 完全匹配计数 | 不使用此指标 | 0/20 |
| 计数误差≤1次 | 不使用此指标 | 4/20（20.0%） |
| 条件MAE | 173.51 kcal（n23） | 6.40次（n10） |
| 条件RMSE | 210.96 kcal | 7.80次 |
| 条件MAPE | 87.02%（n23） | 46.14%（n10） |
| 相对误差≤20% | 4/24 | 不使用此指标 |
| 平均单次墙钟时间 | 4.57秒 | 8.90秒 |
| 确认/重复写入检查 | 24/24通过 | 固定测试＋真实浏览器闭环单列 |

分母包含弃权时，未给值一律不算准确；不将null当0。条件误差只对给完整值的样本计算，必须同时看覆盖率。食物项漏识别没有独立语义标注，整餐热量误差能受漏项影响，不能由这些数字推出识别召回率。

### 餐食逐图重复结果（kcal）

| 素材ID | 参考值 | 第1次 | 第2次 | 第3次 | 预测样本标准差 |
|---|---:|---:|---:|---:|---:|
| dish_1558375732 | 45.54 | 120 | 120 | 未知 | 0.00 |
| dish_1560444967 | 197.38 | 325 | 390 | 370 | 33.29 |
| dish_1561667238 | 268.80 | 610 | 660 | 700 | 45.09 |
| dish_1563389626 | 498.62 | 200 | 200 | 135 | 37.53 |
| dish_1563565354 | 272.32 | 230 | 380 | 310 | 75.06 |
| dish_1563984140 | 245.44 | 150 | 250 | 120 | 68.07 |
| dish_1565123783 | 93.06 | 250 | 300 | 250 | 28.87 |
| dish_1565896745 | 368.54 | 285 | 225 | 305 | 41.63 |

### 视频逐段结果（次）

| 动作 | 素材ID | 作者标注 | 第1次 | 第2次 | 弃权原因 |
|---|---|---:|---:|---:|---|
| sit_up | stu1_60 | 12 | 1 | 1 | — |
| pull_up | stu2_39 | 6 | 7 | 7 | — |
| sit_up | stu3_57 | 19 | 10 | 10 | — |
| squat | stu5_68 | 20 | 10 | 10 | — |
| push_up | stu7_45 | 3 | 需核对 | 需核对 | 存在较长遮挡或缺失片段；画面中多人，无法可靠绑定同一训练者 |
| push_up | stu8_45 | 11 | 需核对 | 需核对 | 画面中多人，无法可靠绑定同一训练者 |
| bench_press | test135 | 4 | 3 | 3 | — |
| pull_up | test1454 | 6 | 需核对 | 需核对 | 关键关节可见率不足 |
| squat | test2315 | 3 | 需核对 | 需核对 | 关键关节可见率不足；存在较长遮挡或缺失片段 |
| bench_press | train151 | 3 | 需核对 | 需核对 | 关键关节可见率不足；存在较长遮挡或缺失片段；画面中多人，无法可靠绑定同一训练者 |

两次视频输出总数/弃权决定逐段一致；这只说明本机重复稳定，不能证明正确。主要可观察失败是少计和弃权，个别多计；拍摄视角、动作幅度和姿态缺失是后续需要定向验证的原因，当前未据保留结果反调门槛或删除难例。

### 开发集与旧结果

原视频v1开发5段、原始角度轨迹、日志及v2重评分保留在`video-dev-v1/`、`video-dev-v2-rescore.json`，首次包装脚本UTF-8读取失败也保留，没有当作模型失败或覆盖原输出。v2正式开发批次为5段：39次俯卧撑准确、深蹲3/4、引体1/7，仰卧起坐/卧推弃权。
原餐食提示词开发4/4不给完整热量；v2开发4/4给估计、MAE45.01kcal、MAPE51.56%。这是开发过程记录，不能和保留集混合或声称由提示词带来独立泛化提升。

## 工程与执行验证

- Windows完整固定响应回归：166/166，包括真实测试库、原文本流程、媒体归属/参数、完整解码、周期/半次、遮挡/多人、取消子进程回收、不转发云密钥、错误素材SHA/时长拒绝、不可变自动证据、确认/幂等。软件测试不等于视觉准确率。
- 真实浏览器：12项检查通过，使用开发集39次俯卧撑视频，无云调用；上传→本地数39→本人改40→核对时不丢更正→确认前0条→确认后40次，自动证据仍39；刷新可查、手机无横向溢出、退出清理、无JS异常。首次检查脚本漏传查询日期失败报告保留，修正后独立新目录重跑。
- Linux容器：failed；证据`artifacts\media-v2\26d17521-34fd-4064-83ad-59c0e360b5f1\container-checks\0a79546b-d1f8-4ef7-9e52-76f6fa4ffdb7\report.json`；构建阶段未完成，未进入运行检查。
- Linux容器：failed；证据`artifacts\media-v2\26d17521-34fd-4064-83ad-59c0e360b5f1\container-checks\9b0ac535-f6ea-405e-9ee2-f249621ad246\report.json`；构建阶段未完成，未进入运行检查。
- Linux容器：failed；证据`artifacts\media-v2\26d17521-34fd-4064-83ad-59c0e360b5f1\container-checks\cb65f754-f41b-4330-a912-bddfc12d60fc\report.json`；构建阶段未完成，未进入运行检查。
- Linux本轮**待验证**：3次构建尝试受HTTP/TLS或不完整下载哈希失败阻断，未绕过校验；末次长时间传输未完成后结束专用构建，原失败及清理记录保留。首次验收脚本还发现相对挂载路径问题，已改成绝对路径并独立核查没有残留资源。未获得Linux测试/计数/新运行镜像成功证据，不能借用旧镜像通过结论。
- 额外真实WebM空场景检查：30帧解码到EOF且不给虚假的总次数，结果见`webm-check/result.json`。
- Windows `pip check`、项目Ruff检查/134文件格式、JS语法与官方权重SHA验证通过。新增本地媒体依赖未替换既有NumPy等版本；本轮未重建Conda、改旧项目、改简历、发布代码或运行新云CI。

计时条件：本机Windows11、现有fit Python3.12.16，CPU本地计数、单批次并发1；保留集餐食和视频批次不重叠，开发批次曾并行且不用于延迟对比。餐食墙钟包含上传/FFmpeg/网络推理/自动核对确认/数据库与清理，视频包含子进程启动/解码/姿态/计数/轨迹写入。不是端到端用户等待SLA，未控制供应商负载或所有宿主机进程。具体CPU和版本在各批次protocol.json。真值验证与评分不传给模型，自动核对确认只是评测脚本扮演用户，不代表生产模型可以确认写入。

## 费用与原始证据

| 批次 | 请求数 | 输入Token | 输出Token | 已知估算人民币 | 用量未知 |
|---|---:|---:|---:|---:|---:|
| food-dev-48180ab0-c7ae-4b5a-a3a2-ba462b134685 | 4 | 3000 | 589 | 0.002678 | 0 |
| food-dev-d893aac8-068c-42dc-871f-e9fe16c2fe63 | 4 | 3544 | 799 | 0.003370 | 0 |
| food-heldout-7360ea46-6e11-48d5-a9dc-9dd3910175f9 | 24 | 21264 | 4493 | 0.019618 | 0 |

本轮餐食共32次、已知估算约**0.025666元**；视频计数0次云调用。按[硅基流动公开价格](https://www.siliconflow.cn/models)冻结输入0.5元/百万Token、输出2元/百万Token计算；实际实扣未知，不代替平台账单。每批上限5元是保守保护，不是花费或充值；旧实验账本不重置、不并账。

证据目录：`artifacts\media-v2\26d17521-34fd-4064-83ad-59c0e360b5f1`，入口`artifacts/media-v2/current.json`。
包含`results.json/csv`、逐批`protocol.json/runs.jsonl/summary.json`、餐食`responses/`与`budget.json`、视频逐段`*-result.json/*-features.json`、开发修正前后与冻结源码/最终源码。真实素材与官方原始标注在`data/media-eval-v2/`，本机研究保留。旧架构/文字/中断实验全部保留，不拼接成本轮成绩。

## 运行与复现

在项目目录使用现有fit解释器。首次只需显式安装本地计数依赖和权重：

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m pip install '.[media]'
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.pose_setup --download
& .\scripts\start_siliconflow.ps1
```

打开页面“图片与视频”，选择“力量训练视频计数”及实际动作，再上传完整单动作视频；未知次数可留空，核对后再生成候选并确认。餐食选择“识别餐食照片”，核对食物、重量和热量；仍是估算来源。

素材校验或新机器按冻结清单重新获取（视频下载工具需`.[media-eval]`；只下载所选样本）：

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.media_data --dataset nutrition5k
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.media_data --dataset repcount
# 缺少数据时加 --download；缺Lance工具时先安装 '.[media-eval]'
```

新评测每次创建新UUID目录，不覆盖旧批次，默认无付费调用；餐食必须显式--live：

```powershell
$env:FITLIFE_MODEL_PROFILES_FILE = 'H:/Tomorrow/FitLife/config/siliconflow.models.json'
$env:FITLIFE_VISION_MODEL = 'vision'
$env:SILI_API_KEY = [Environment]::GetEnvironmentVariable('SILI_API_KEY', 'User')
$env:FITLIFE_REDIS_CREDENTIALS_FILE = 'E:/Work/Config/Redis/.env'
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.media_eval --task food --split heldout --repeats 3 --live
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.media_eval --task video --split heldout --repeats 2
```

最终评测脚本仅补充了Windows清单路径在Linux的兼容读取，UI补更正保留与核查脚本，未改冻结的计数、提示词或评分；原冻结源码仍独立保存。

这些样本现已用于当前报告；继续按结果优化后只能叫已见回归集，新的泛化结论需另取未见数据。复现原实现先解压`source-evaluation-frozen.zip`至独立目录，不将其覆盖现有项目。

软件与浏览器/容器检查（不消耗云Token）：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& E:\Work\Envs\Conda\fit\python.exe -m pytest -p pytest_asyncio.plugin --integration -q
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.video_ui_check
& E:\Work\Envs\Conda\fit\python.exe -m scripts.media_container_check
```

当前结论：已实现并验证可恢复、需确认的媒体业务流程，已形成可复现的真实质量基线；**准确率没有达标证据**。后续优先解决计数表征/视角与遮挡、增加用户可提供的餐食份量依据，再用新的未见集验证；不把软件回归通过或低费用当成识别效果。
