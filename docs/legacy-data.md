# 数据迁移与历史资料

2026-11-03 用户明确要求把有用数据全部搬到新版，后续不再依赖旧项目。本轮已完成；旧目录保持原样，不作为开发或运行入口。

## 已搬迁的数据

| 数据 | 条数 |
|---|---:|
| 食品资料 | 41 |
| 饮食记录 | 102 |
| 身体测量 | 33 |
| 训练记录 | 19 |
| 步数记录 | 33 |
| 身体档案 / 训练偏好 / 训练日状态 | 1 / 1 / 2 |
| 历史会话 / 消息（含原工具轨迹和待处理字段） | 8 / 30 |
| 历史知识资料 | 17 |
| 照片餐食 | 0 |
| 合计 | 287 |

这287条已写入开发MySQL的独立历史资料空间，保留原字段、空值、日期、原始JSON、原ID及关联。餐食→食品、消息→会话的引用检查无缺失。相同数据包重复导入新增0行；逐条从MySQL读出后重新计算的数据集哈希与迁移包一致。不会以历史年龄代替当前年龄，不把旧训练偏好直接变成当前约束，不运行历史聊天的待确认操作。

41条食品分级保留：包装标签声明11、仅“rag”来源29、模型图片识别待核验1。均未重新核对营养原始出处。历史餐食保留原先的营养快照，不把这些数值宣称为新版重新计算或校验过的结果。

17段公开营养资料另已接入现有知识检索：CNS指南8、WHO健康饮食7、WHO减钠2。与旧JSONL、文本和3份HTML来源哈希核对一致，保留网页版本、旧采集日期、URL及内容哈希；这是历史快照的一致性检查，没有重新联网核对网页现状或审核个体医疗适用性。

## 现在的数据位置

- `data/history-v1/`：独立完整数据包。`records.jsonl`为287条可移植数据；`manifest.json`含表结构、条数、原始关联、文件哈希和历史空间UUID；`originals/`为两份只读SQLite源库快照，`reference/`保留公开原文及食品待审清单。
- MySQL：`fitlife_dev.fl_domain_records`中`record_type=legacy_archive`为历史记录；单独`fl_pending_actions`记录一次`archived`迁移事件，不作为待确认操作；`fl_knowledge_chunks`为原有资料检索入口。
- API：`GET /history`、`GET /history/{table}?offset=0&limit=30`；身份由个人令牌确定，不接受用户ID来跨用户查询。
- 界面：“历史资料”页按类型查看，支持分页。历史聊天留作档案，不混入当前模型上下文。当前Agent业务工具仍处理新版当前事实；资料搬迁不代表已把所有历史字段投影为当前业务事实。
- 本地证据：`artifacts/legacy-archive-20261103/report.json`、`tests.xml`、`regression.xml`；资料文件均被Git忽略，未上传GitHub。

旧库与新库结构不同，使用既有DomainRecord的独立历史类型保留原始事实，避免为旧字段扭曲新业务表、伪造确认事件或覆盖已有用户。历史空间单独归属一个新用户，当前空间无历史资料时并非迁移失败；不会猜测应绑定哪个已有账户。

更早数据库备份、重复的旧复现导出、未被数据库记录引用的媒体/抽帧缓存不属于本次有效数据集，未复制。源库快照保留SQLite内部结构，运行查询使用MySQL。没有恢复、启动或安装旧应用。

## 后续只使用新版

恢复/再次导入不需要旧目录。在`H:\Tomorrow\FitLife`运行：

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.legacy_archive import .\data\history-v1 --environment dev
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.knowledge .\data\history-v1\reference\knowledge.jsonl --environment dev
```

查看历史空间时，在你自己的终端签发该空间的令牌，再填入网页；不要把令牌发到聊天：

```powershell
$historyOwner = (Get-Content .\data\history-v1\manifest.json -Raw | ConvertFrom-Json).default_owner_id
& E:\Work\Envs\Conda\fit\python.exe -m fitlife.manage --environment dev --user-id $historyOwner
```

合成旧库的抽取、档案完整性、鉴权隔离测试：

```powershell
& E:\Work\Envs\Conda\fit\python.exe -m pytest -p pytest_asyncio.plugin tests\test_legacy_data.py tests\test_legacy_archive.py --integration -q
```

前一阶段`data/legacy-import-20261103`及其报告保留，记录最初17段资料、41条待审食品的选择性抽取。该阶段“个人记录未迁移”的结论是当时范围；本轮用户追加授权后已完成上述历史资料迁移。历史指标不转成新版成果，真实个人历史不加入开发/保留评测集。
