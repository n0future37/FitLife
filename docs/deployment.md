# 本机、容器与CI交付

本机命令以README为准。迁移head为0009_conversation_titles；开发/测试库实际迁移和用户配置以STATE为准。
开发凭据已由用户在本机隐藏配置，manage仍可签发新的本机个人令牌。server不自动迁移，版本不匹配拒绝启动。
不用root做业务连接，不自动接管没有fl_alembic_version的旧表。

## Docker Desktop / Linux

镜像：Python3.12.16-slim，固定已验证基础摘要；以UID10001非root运行，业务产物写/app/artifacts，私有媒体写/app/data；镜像包含FFmpeg/FFprobe。
构建上下文只允许代码、迁移、测试、合成评测与依赖元数据，不含.env、旧main/.idea、个人数据或artifacts。
测试镜像另含开发工具；运行镜像不安装pytest。Windows专属pywin32按平台标记排除。

```powershell
docker build --target runtime -t fitlife:0.6.0 .
docker build --target test -t fitlife:test-0.6.0 .
```

compose.yaml只启动FitLife应用，连接现有host.docker.internal:3306/6379，不重建公共MySQL/Redis。
公开端口仅绑定127.0.0.1:8100，产物绑定项目artifacts/container，私有媒体绑定data/container。MySQL仍配fit_dev/fitlife_dev，Redis DB2。
需要在**当前本机进程**准备三个秘密变量，Compose从环境创建只读secret文件；不把值放进配置或镜像。
MySQL可通过本机凭据管理器/隐藏输入，Redis取已有安全配置，模型取本机密钥；不要运行会展开配置的命令输出秘密。
进程已有这些安全值后执行`docker compose up -d`，停止用`docker compose down`。
环境来源secret与本机Compose的只读根文件系统不兼容，本配置采用可写根文件系统，
保留非root、去掉所有capabilities、禁止提权、只读secret与本机端口绑定。

秘密文件入口：FITLIFE_DB_PASSWORD_FILE、FITLIFE_REDIS_PASSWORD_FILE、FITLIFE_KEY_FILE。
普通本机运行仍支持进程变量和Windows凭据；模型密钥不会传给MCP工具子进程。

容器验收：本机先指定FITLIFE_REDIS_CREDENTIALS_FILE为已有配置路径，使用fit解释器
`-m scripts.container_check`。它只读取test项目凭据，建立独立随机Compose项目、挂载只读secret，
运行Linux测试、检查/ready/页面/未认证阻断/非root，最后关闭本次容器与网络。
不会停止公共数据库、Redis或其他应用。失败原报告保留。

## 历史0.4验收（不充作0.5证据）

2026-11-02：首次因Compose环境来源secret与只读根文件系统不兼容而失败，修正后通过。
artifacts/container-checks/57e289e4-13e3-4ade-aa70-92cede14557a/report.json，
Linux JUnit 24项、0失败/错误；MySQL/Redis真实连接、认证API/MCP回归及UID10001启动均通过。
这是固定响应容器验收，不声称在容器内做了付费真实模型评测。临时容器/网络已关闭。

最终Linux业务回归25/25：artifacts/container-checks/b6234ef6-28e7-4545-822d-a18561b61a8c/linux.xml。
页面退出与恢复收尾后，运行镜像再次构建并按当前app.js哈希、就绪、认证阻断、非root复验通过：
artifacts/container-checks/1b53f1d2-7a27-4143-9d5f-934b3efb17eb/report.json。
镜像ID与完整包版本快照随交付放入artifacts/releases，不将旧测试镜像的UI文件当作最终页面。

## 持续集成

.github/workflows/checks.yml包括静态检查、格式检查、MySQL/Redis集成、固定响应原核心和新编排的开发/保留回归、
镜像构建和测试证据归档，**不使用模型密钥或真实模型费用**。
CI MySQL/Redis为作业内全新一次性服务，仅回环端口；空密码root仅用于建立随机密码的fit_test，
不适用于现有本机或生产环境。脚本检查GITHUB_ACTIONS且专用于全新库；不用它改本机账号。
个人访问令牌只以摘要存储；本机签发命令只显示一次，不写产物。

本机Windows与Linux容器执行了对应核心检查；云端运行结果以[GitHub Actions](https://github.com/n0future37/FitLife/actions/workflows/checks.yml)的具体run为准。
初次上传前不会将已有工作流配置当作云端CI通过；原开发机STATE记录审批和实际run链接。
工作流使用官方checkout/setup-python/upload-artifact，参考其官方README和GitHub服务容器文档。

源码/环境冻结：本机fit解释器`-m scripts.freeze --name fitlife-0.6.0-20261103`。
它只归档代码、合成用例、公开配置示例和说明，不含.env、业务数据、令牌或运行原文。
评测旧包的复现补充与独立解压检查见evals/BASELINE.md。

## 运行限制

当前提供本机部署，基础个人令牌不是SSO、OAuth或生产注册体系。公网部署需要明确的HTTPS、
账号接入、备份/运维与实际容量验收；当前不发布公网服务。
对话可由用户删除，偏好可确认忘记；已确认计划/活动与操作证据按业务保留，尚无合规级全量抹除服务。
模型调用数、输入字节、输出及时间有界；token单位是保守预算账本，账单费用未知时保持null。
容器依赖环境与本机可能有不同传递依赖，需保留镜像ID及版本快照，不以直接依赖文件代替完整环境证据。

依据：[Docker Secrets](https://docs.docker.com/compose/how-tos/use-secrets/)、
[Docker Python指南](https://docs.docker.com/guides/python)、
[GitHub服务容器](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers)。

## 2026-11-03完整度收尾

当前源码Windows/Linux各125项通过，公开模型配置按明确清单进入镜像；配置只有环境变量名称，不含密钥值。新镜像fitlife:readiness-v1和fitlife:test-readiness-v1，旧0.6标签保留。本轮未上传或运行新云CI，结果和首次失败见[核查报告](readiness-v1.md)。

本轮Linux使用固定模型和测试服务，不证明容器内真实硅基流动/本地故障切换已配置。容器内local.base_url的127.0.0.1指向容器自身，连接宿主Ollama时需另行设置可达地址；Windows本地兜底已有独立实测。


## 2026-11-16多模态运行增量

当前Docker默认runtime及test阶段包含`.[media]`（MediaPipe、NumPy、OpenCV）与系统视频库。
Compose额外只读挂载`./data/pose-models:/app/data/pose-models:ro`；姿态权重不烘焙进镜像，
先执行`python -m fitlife.pose_setup --download`并核对固定SHA256。
图片分析仍需已有视觉模型配置；指定动作计数在独立本地子进程执行，不读取云模型密钥。
复验命令`python -m scripts.media_container_check`使用独立项目和`fitlife:media-v2`、
`fitlife:test-media-v2`新标签，保留历史标签；仅操作测试库合成用户。
真实精度与当前验证证据见[media-v2](media-v2.md)，不得用旧125项/Linux/云CI代替本次结果。
