# 周计划与进度

输入是指定周一、已确认画像、现有计划和本轮明确修改。服务端按规则生成最多七次草案，
模型负责理解要求和调用工具，日期安排与完成率由程序计算。这是可解释的规则基线，
不是医学处方，也不声称个性化训练最优。

`GET /plans/{week_start}`读取当前版本，`POST /plans/actions`提交草案请求
（week_start、expected_plan_version，以及可选activity_id/available_weekdays/weekly_sessions/session_minutes）。
首次版本为0，确认后为1。`GET /plans/{week_start}/progress`查询进度，接口均需本人令牌。
MCP必须先get_current_plan才能propose_week_plan，缺信息时返回needs_information。
本轮临时覆盖不修改长期画像；未指定字段优先保留现有计划，再采用画像。

候选沿用确认/拒绝/修改接口，绑定画像版本、计划版本、内容摘要和有效期。
修改后旧确认失效；原计划或画像变化后旧候选失效。计划与不可变历史版本在同一事务保存，
重复确认只返回同一版本。规则相同且无变化时不产生多余候选。

完成率按同日同项目匹配分钟数，每次以计划分钟数封顶。例：计划周一步行20分钟，
周一步行15分钟计75%；周二额外步行不冒充周一完成；未记录不等于实际未运动。
额外时长、部分完成、已完成次数分别返回，尚不做自动调整或删除历史活动。

验收：使用fit解释器执行`-m fitlife.plan_check`，固定模型响应+真实MySQL/MCP；
加`--live`另用真实模型生成计划和查询进度（产生费用）。只访问fitlife_test，
清理本次随机用户数据并保留迁移。测试库现迁移至0003_weekly_plans，开发库未迁移。

2026-11-02规则检查报告b48f53da-8a96-4f9d-991f-232b13b19894，
真实模型报告5e802d0c-8810-4a33-8440-a39044580cf3（artifacts/plan-checks）。
后者7组检查通过、清理验证通过；两次真实任务仅是模块验收样本，不作稳定成功率推断。
合成进度回答已由开发助手复核：20分钟计划、15分钟匹配、75%且未宣称写入，
与规则和数据一致。此复核不是用户人工标注或独立模型评分，原报告保留pending原始状态。

MySQL密码可由本机`-m fitlife.credentials --environment test`隐藏输入并保存到
当前Windows用户凭据管理器。只读取FitLife专属条目，不枚举其他凭据；加`--delete`删除。
已配置后验收无需重复输入，跨平台使用进程环境FITLIFE_DB_PASSWORD，均不写项目文件。
