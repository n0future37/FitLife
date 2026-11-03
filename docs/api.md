# 认证API与业务Agent

## 当前实现与数据流

用户携带个人访问令牌 → API验证摘要/有效期/撤销状态 → 服务端创建Actor → 请求编号去重 →
单Agent通过私有stdio MCP查询本人数据或提出候选 → 用户单独调用确认接口 → MySQL事务保存。

| 模块 | 职责 |
| --- | --- |
| auth.py | 32字节随机个人令牌，数据库只保存SHA256摘要；默认24小时有效，支持撤销 |
| api.py | Bearer认证、请求校验、资源归属、业务HTTP入口及Agent入口 |
| requests.py | 用户/请求编号唯一、消息摘要绑定、完成结果复用、执行租约和过期执行者隔离 |
| business_mcp.py | 从可信父进程获得身份/请求键，工具参数没有user_id，也没有确认/拒绝工具 |
| agent.py | 根据上下文选择工具白名单，候选直接由服务端返回，个人轨迹脱敏 |
| manage.py / server.py | 本机迁移/用户令牌管理，以及仅监听127.0.0.1:8100的服务启动 |

### 接口

- GET /health：进程存活；GET /ready：MySQL及启用的Redis就绪；/页面及/assets为公开静态资源，业务接口需Bearer认证。
- GET /me、GET /profile、GET /activities?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD。
- POST /profile/actions、POST /activities/actions：提出候选，需Idempotency-Key（UUID）。
- GET /actions/{id}；PATCH /actions/{id}（revision、digest、新payload）。
- POST /actions/{id}/confirm 或 /reject：提交精确revision和digest，仍由既有事务服务验证归属/状态。
- POST /agent/requests：body为message和可选conversation_id，需Idempotency-Key（UUID）；不接受客户端指定用户、模型或离线开关。
- DELETE /auth/token：撤销当前个人访问令牌。

模型还可查询/提出周计划、查询进度、提出活动删除和忘记画像字段；共10个工具。
周计划/会话/分页和删除入口详见plans.md、conversations.md、engineering.md及服务的/docs接口文档。
一轮最多返回一条待确认操作；多个活动应分开提交。生成候选后执行器直接停止，返回pending_confirmation，
不让模型继续调用其他工具或谎称已保存。确认/拒绝/修改属于用户交互入口，不授予模型。
CLI公开目录仍可独立运行，业务权限仅经已认证API进入。

### 重复请求与恢复

同用户同请求编号绑定消息摘要；完成后复用结果，不再次调用模型。不同消息复用编号返回409。
运行中重复请求返回409；执行租约过期后可重新取得执行权，旧nonce不能覆盖新结果。
业务候选也绑定同一请求键，重试不会产生重复候选或活动；已有候选内容与重试不同则报冲突，不覆盖。
缓存返回候选时重新读取最新状态，已确认操作不会再显示为待确认。

现有多轮会话、上下文快照及候选中断恢复，见conversations.md；不是任意代码位置精确续跑。
每进程最多两个模型任务，等待满2秒429；正式server还用Redis共享容量限制。
同步数据库通过线程执行、独立Session；不是全异步数据库栈或已验证的生产吞吐。

## 凭据和启动

MySQL密码只给后端连接；个人访问令牌只给API客户端；模型平台API Key只给模型调用。三者不是同一个凭据。
工具不会收到API客户端令牌或模型Key；私有MCP子进程只接收必要数据库凭据和已验证的身份/请求上下文。

本机正式开发入口（会隐藏读取fit_dev密码，尚未代为迁移开发库或创建长期用户）：

```powershell
Set-Location H:\Tomorrow\FitLife
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.manage --environment dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.server --environment dev --redis-credentials-file E:\Work\Config\Redis\.env
```

manage执行增量迁移并创建用户/签发令牌；原文只在本机终端显示一次，不写日志或文件。
用--user-id为已知用户签发新令牌；不是对外注册/密码登录/OAuth服务，也不声称完整企业身份系统。
server不自动迁移，schema不匹配则拒绝启动，默认关闭访问日志。当前仅本机使用，外网部署/TLS另行实施。

## 已验证证据（2026-11-02）

用户本机执行 `python -m fitlife.api_check --live`，测试账号fit_test、库fitlife_test。
已读取报告artifacts/api-checks/9d000f15-2696-4956-afe1-2e71398f4184.json，10组检查通过，
fixture_cleanup=verified；在该阶段曾核对源码哈希，后续版本已有扩展，原快照保留。迁移0002增加两张表。

覆盖：摘要令牌、未认证/无效/过期/撤销、归属和单次写入、画像接口、真实MCP候选→确认、
缓存重放零额外模型调用、模型不能自确认、租约恢复/旧执行者阻断、真实模型记录/查询副作用及日志脱敏。
固定响应检查不当作模型质量分数；租约恢复用故障注入模拟，不是杀进程或多轮恢复完整测试。

真实样例使用ModelFlare/gpt-5.6-sol：记录请求1次模型/1次propose_activity，查询2次模型/1次query_activities。
运行ID为c45e355e-e916-45ab-86a3-3389be28124c和d656c149-c252-452b-b8af-375ca1f9e584。
确认前活动0条，用户接口确认后1条/20分钟，查询后仍1条；开放式最终回答仍待集中评测，不计为已人工评分。
15.506/20.013秒仅为本次两条Agent进程观察，不是HTTP全链路性能或稳定速度；实际账单费用未知。

普通运行日志不保存个人工具参数/结果、用户原话、候选payload、完整回答或凭据；
必要候选和API结果存MySQL并按用户隔离。聊天删除清除正文/上下文/缓存；业务证据保留边界见conversations.md。

## 实现讲法与追问（个人贡献以本人实际参与为准）

“实现用服务端认证上下文约束Agent工具权限，让模型只能查询本人数据和提出操作候选。
确认保存是独立用户接口，结合数据库事务/版本和请求幂等，保证重复请求不绕过确认或重复写入。”

为什么不让模型传user_id？它属于不可信参数，身份必须来自已验证的令牌。
为什么模型说确认也不能保存？它没有确认工具，程序在产生候选时终止执行；只有用户接口能进入确认服务。

参考：https://fastapi.tiangolo.com/reference/security/ 、https://fastapi.tiangolo.com/async/
