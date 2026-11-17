# 演示截图与数据来源

生成日期：2026-11-17。截图来自当前代码在真实 Chrome 页面中的验收，不是设计稿。

| 文件 | 展示内容 | 素材与响应 |
| --- | --- | --- |
| images/plan-adjustment.png | 计划调整前后差异、数据依据与确认 | 临时合成用户，固定模型回复 |
| images/chat-confirmation.png | 补充步行时长、生成候选、等待确认 | 临时合成用户，固定模型回复 |
| images/activity-mobile.png | 移动端活动录入与查询入口 | 同一合成用户，真实网页 |
| images/media-review.png | 上传、后台分析与核对入口 | 合成色块，固定视觉响应 |

截图不包含账号、令牌、模型密钥、私人记录、用户图片或视频。日期和“步行20分钟”均为合成演示数据，真实识别准确率需看独立实验。媒体演示返回的营养信息是测试夹具，不是从色块识别得到的食物结论。

## 复现

先按主 README 配置自己的 fitlife_test / fit_test 和 Redis 测试凭据，安装 Chrome 及 dev 依赖，再在项目根目录运行：

```powershell
python -m fitlife.ui_check
python -m fitlife.media_ui_check
python -m fitlife.adjustment_ui_check
```

检查程序只创建随机合成用户、使用固定模型响应；正常结束后定向清理，并把截图与报告写入本机被忽略的 artifacts/ui-checks/。没有云模型调用。公开截图由其中选取并目视审阅，不上传原报告中的运行细节。

截图显示候选与核对界面；业务最终状态由测试断言和数据库核查验证，不能仅靠截图证明无重复写入或模型质量。
