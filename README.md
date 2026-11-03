# FitLife 0.6.0

个人运动、饮食与身体记录助手。自然语言请求由主Agent分解给三个领域Agent，
工具读取本人事实、生成可核对候选，用户确认后由MySQL事务保存。应用提供一般习惯辅助，不作诊断、治疗或康复决定。

例如“记录今天体重70公斤、步行20分钟”：身体和运动Agent分别提出候选，
主Agent汇成一份变更；拒绝则全部不保存，确认后两项原子写入，重复确认不重复记录。

## 功能与模块

| 模块 | 当前职责 |
|---|---|
| 主Agent与三个子Agent | 意图清单、覆盖校验、依赖执行、前序证据、综合回答、共享预算 |
| 画像与会话 | 唯一确认画像、来源/版本/更正/忘记、临时约束有效期、会话与恢复 |
| 运动 | 周计划草案/历史/进度，活动、步数、训练和反馈，确认删除 |
| 饮食 | 食物库、营养代码核算、餐食历史快照、照片估算、饮食偏好 |
| 身体 | 身高/填写时年龄/性别，按日期体重/体脂/腰围/睡眠/疲劳 |
| 模型 | 独立平台/协议/凭据/能力配置，文字模型选择，Responses与Chat适配 |
| 图片与视频 | 私有上传、后台任务、取消/重试、观察结果核对、版本绑定候选 |
| 评测与工程 | 固定/真实模型分开，数据库副作用规则、私有轨迹、Docker与CI配置 |

默认`master`按依赖运行，`master_serial`为串行对照，`master_independent`保留旧A的独立领域策略，
`single`保留原0.4运动10工具基线。迁移当前主从职责与执行机制，不照搬旧SQLite、Streamlit状态、个人媒体或密钥。
旧0.4源码与指标保留为历史基线，不能作为本次新编排成果。

0.6重点是记录驱动的计划调整、冻结任务评测和真实进程恢复/双实例验证，见[核心交付](docs/core-v06.md)。
调整草案展示前后差异和依据，只修改未来可调整项；确认前重新核对活动、画像与计划版本。

能力成熟度、未迁入的旧实验和验证局限见[capabilities](docs/capabilities.md)。
视频当前为最多3分钟的稀疏采样观察，不承诺全程准确计数；知识资料未导入时明确返回未配置。
未安装本地模型或新增第二平台账户，不能把配置示例当作已经验证。

## 本机启动

沿用fit解释器，不依赖Conda终端自动激活，不重建环境。开发库和测试库密码通过Windows凭据管理器读取。
当前验收范围见[核心交付](docs/core-v06.md)，原开发机详细路径在本机STATE.md。首次需要密码时在本机交互终端隐藏输入，不发到聊天：

```powershell
Set-Location H:\Tomorrow\FitLife
$env:PYTHONIOENCODING = 'utf-8'
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.credentials --environment dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.manage --environment dev
```

manage校验fit_dev/fitlife_dev身份、迁移并签发24小时个人令牌，只在本机显示一次。
已有用户续签加`--user-id 用户UUID`。令牌只填网页，不填模型密钥；退出会撤销当前令牌。

模型默认读取本机`MODELFLARE_API_KEY`。若当前终端没有，使用隐藏输入：

```powershell
$credential = Read-Host '模型密钥' -AsSecureString
$env:MODELFLARE_API_KEY = [System.Net.NetworkCredential]::new('', $credential).Password
Remove-Variable credential
```

```powershell
$env:FITLIFE_REASONING_EFFORT = 'low'
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.server --environment dev --redis-credentials-file E:\Work\Config\Redis\.env
```

访问 http://127.0.0.1:8100/ 。server检查迁移版本，不自动迁移；默认同时处理持久化媒体任务，
在页面点击分析会调用视觉模型产生费用。`/health`检查存活，`/ready`检查数据库与Redis。
Ctrl+C停止。`--local-limit`仅用于显式单进程容量模式，不能据此声称跨进程保护。

## 测试与评测

测试只使用fitlife_test/fit_test、Redis DB3及项目环境前缀；结束后只清理本次随机合成用户的数据。
FFmpeg和FFprobe需在PATH，本机已具备；Linux镜像包含FFmpeg。

```powershell
$env:FITLIFE_REDIS_CREDENTIALS_FILE = 'E:\Work\Config\Redis\.env'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& E:\Work\Envs\Conda\fit\python.exe -m ruff check fitlife migrations tests scripts
& E:\Work\Envs\Conda\fit\python.exe -m ruff format --check fitlife migrations tests scripts
& E:\Work\Envs\Conda\fit\python.exe -m pytest -p pytest_asyncio.plugin --integration
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate --split dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate --split holdout
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate_multi --split dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.evaluate_multi --split holdout
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.ui_check
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.media_ui_check
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.adjustment_ui_check
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.task_eval --split dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.task_eval --split holdout
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.process_check
```

以上使用固定响应，不花模型费用。界面检查使用独立临时Chrome，不读取日常浏览器资料。
真实小额验收需显式运行`-m fitlife.paid_check --live --budget-usd 1.5`；
共用持久化预留账本，重跑不重置预留，实际账单未核对时保持未知。
完整说明见[evaluation-v05](docs/evaluation-v05.md)，当前实测结果见STATE。

开发库迁移与启动验收使用`-m scripts.dev_check`（短暂启动并关闭独立服务，不签发令牌）；
Linux全量验收使用`-m scripts.container_check`，仅建立并清理本次随机容器项目。

## 工程边界与文档

- MySQL保存权威事实，SQLAlchemy/Alembic管理事务与迁移。Redis开发DB2、测试DB3，必须有fitlife:环境:前缀；逻辑DB不是权限隔离。
- 模型不能确认写入。候选绑定内容、版本与有效期，拒绝、修改或源数据变化使旧确认失效。
- 每个领域每轮至多提出一个候选；部分领域失败不保存部分业务变更，用户可分开提交。
- 临时约束有名称、日期和领域；过期不再送入旧上下文。结构化计划限制由服务端核验；本轮明确例外需在草案展示并确认。
- 基础个人令牌用于本机学习，未提供公网账号/SSO、生产容量或医疗质量保证。没有对外发布或提交Git。
- 上传源文件可删除，聊天和偏好可管理；已确认事实和必要操作证据保留，不是合规级全系统抹除。
- 普通运行日志脱敏，不保存私人原文或媒体；评测合成素材单独标记。未知用量/费用不按零计算。

[架构与迁移](docs/agent-migration.md) · [模型配置](docs/model-profiles.md) · [部署与CI](docs/deployment.md) ·
[计划规则](docs/plans.md) · [会话](docs/conversations.md) · [工程取舍](docs/engineering.md)。
